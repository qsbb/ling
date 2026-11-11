"""生成媒体资产的**唯一写入方**：保存、读取、导出、过期清理。

## 一份产物的一生

```
   TTS 产出 bytes
        │
        │  save_bytes()  ── 1. 落盘（原子，storage.put）
        │                    2. 插记录（ready_at 已设）
        ▼
   media_assets 一行  ── digest = sha256 = 文件名
        │
        ├── read_for_window()   授权后读字节（Web 播放）
        ├── export_for_window() 授权后按 transport 导出（渠道发送）
        ├── sign_url_for_window() 授权后签发短时效 URL
        └── purge_expired()     过期清理（先标记、后删文件）
```

## 原子性：为什么先落盘后写库

反过来（先插记录、再落盘）会出现**一段所有读者都能看到、但文件还不存在**
的窗口。那段窗口里的每一次读取都是一次错误路径，而错误路径才是真正会被
"偶发 500"暴露出来的东西。

先落盘则相反：文件先完整存在，然后才有一行指向它。中间的失败（进程被杀）
只留下一个**孤儿文件**（没有记录指向它），它不影响任何读取，且能被
`collect_orphans()` 收拾掉。**孤儿文件是无害的，悬空记录是有害的**——所以
永远选前者。

## 去重与"两次请求同一份内容"

`digest` 唯一。第二次保存同样的字节时：

- 文件已存在 → 不重写；
- 记录已存在 → **更新归属**（`window_id` / `turn_id` 等），不新增行。

但这里有个真实的坑：**同一份音频可能被两个窗口用到**。一行只能记一个
`window_id`，更新归属会让先前的窗口突然失去访问权（它的消息里引用的音频
播不出来）。

处理方式：归属列为空/不同时**不动原有归属**，只把这一行当作"内容已存在"
返回。代价是两个窗口共用同一份文件、而授权只看第一归属——`read_for_window`
因此额外放行一种情况：**同一 `actor_id` 且该 actor 有引用记录**。首版 TTS
的产出按窗口隔离（文本里含窗口上下文），实际几乎不会撞；撞上时宁可让第二个
窗口重新生成（`save_bytes` 返回的 `created=False` 让调用方知道"这不是新
文件"），也不要静默改归属导致第一个窗口播不出来。

## 删除为什么不影响在途任务

删除分两步：**先软删（`deleted_at`）、后删文件**。

- 软删之后所有读取路径都当它不存在（`is_live()` 为假），所以不会有人再
  拿到一个即将消失的路径去下载；
- 文件的实际删除被推迟到清理任务，且在删之前会**重新确认**没有活跃引用；
- 在途任务（已经拿到内容、正在编码/发送）手里是**字节或已签名的 URL**，
  不依赖文件继续存在——签名 URL 才需要文件，所以清理有一个宽限期
  （见 `purge_expired` 的 `grace_seconds`）。

## 保留期

`retention_days` 可配置，默认 7 天。`LING_MEDIA_ASSET_RETENTION_DAYS`
覆盖；设为 `0` 或负数表示**不自动过期**（用户明确要留着）。
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from ling.domains.media.exports import (
    DEFAULT_URL_TTL_SECONDS,
    ExportDenied,
    ExportTarget,
    ExportTransport,
    check_window_access,
    export_as,
)
from ling.domains.media.storage import GeneratedMediaStorage, MediaStorageError
from ling.infra.media_assets import MediaAssetRow, utcnow
from ling.infra.models import MediaEvidenceRow

logger = logging.getLogger("ling.domains.media.assets")

#: 默认保留期（天）。
DEFAULT_RETENTION_DAYS = 7

#: 删除前的宽限期：软删后至少等这么久才真正删文件。
#:
#: 为什么需要：一个已签发的 URL 可能还没被渠道取走（它在 5 分钟内有效）。
#: 立刻删文件会让那条链接 404——而用户看到的是"语音发一半没声音"。
DEFAULT_DELETE_GRACE_SECONDS = 3600

#: 允许的格式 → MIME。**不猜**：格式由产出方声明，这里只做白名单校验。
_FORMAT_MIME = {
    "mp3": "audio/mpeg",
    "wav": "audio/wav",
    "ogg": "audio/ogg",
    "opus": "audio/ogg",
    "silk": "audio/silk",
    "amr": "audio/amr",
    "m4a": "audio/mp4",
}


class AssetError(Exception):
    """资产层的可预期失败。"""


class AssetNotFound(AssetError):
    """资产不存在或**不可见**（已删除/未 ready/已过期）。

    这两种情况**故意合并成同一个异常**：区分开就等于告诉调用方"这个 id 存在，
    只是过期了"，可以用来枚举别人的产物 id。
    """


@dataclass(frozen=True)
class SavedAsset:
    """一次保存的结果。"""

    digest: str
    size_bytes: int
    #: 是否新建了数据库行（False = 同一份内容已存在，复用）。
    created: bool
    relative_path: str


@dataclass(frozen=True)
class AssetView:
    """对外可见的资产描述。**不含磁盘绝对路径**。"""

    digest: str
    kind: str
    format: str
    mime_type: str
    size_bytes: int
    sample_rate: int | None
    duration_ms: int | None
    created_at: datetime
    expires_at: datetime | None


def resolve_retention_days(value: int | None = None) -> int:
    """保留期（天）。显式参数 > 环境变量 > 默认。

    `<= 0` 一律转成 `0`，其含义是"不自动过期"（`purge_expired` 会跳过）。
    """
    if value is None:
        raw: str = os.environ.get("LING_MEDIA_ASSET_RETENTION_DAYS") or ""
        if not raw:
            value = DEFAULT_RETENTION_DAYS
        else:
            try:
                value = int(raw)
            except ValueError:
                logger.warning(
                    "LING_MEDIA_ASSET_RETENTION_DAYS 不是整数，采用默认 %s", DEFAULT_RETENTION_DAYS
                )
                value = DEFAULT_RETENTION_DAYS
    return max(0, int(value))


class GeneratedMediaService:
    """生成媒体资产的唯一写入方。

    ``data_dir`` 与 ``root_key`` 从配置来：前者决定文件放哪，后者是签名
    密钥的来源。两者都不写死，测试可以给临时目录与固定密钥。
    """

    def __init__(
        self,
        session: AsyncSession,
        *,
        data_dir: Path | str,
        root_key: bytes,
        actor_id: str = "",
        base_url: str = "http://127.0.0.1:8080",
    ) -> None:
        self._session = session
        self._storage = GeneratedMediaStorage(data_dir)
        self._root_key = root_key
        self._actor_id = actor_id
        self._base_url = base_url

    @property
    def storage(self) -> GeneratedMediaStorage:
        return self._storage

    # -------------------------------------------------------------- 保存

    async def save_bytes(
        self,
        data: bytes,
        *,
        actor_id: str,
        window_id: str,
        kind: str = "audio",
        fmt: str = "mp3",
        mime_type: str | None = None,
        sample_rate: int | None = None,
        duration_ms: int | None = None,
        subject_id: str | None = None,
        turn_id: str | None = None,
        message_id: str | None = None,
        producer: str | None = None,
        retention_days: int | None = None,
        expected_digest: str | None = None,
    ) -> SavedAsset:
        """把一段字节存成可引用的资产。

        顺序**不可交换**：先 `storage.put`（原子落盘 + fsync），再插记录。
        见模块文档"为什么先落盘后写库"。
        """
        if not actor_id:
            raise AssetError("保存生成媒体必须给出 actor_id：没有归属就无法授权")
        if not window_id:
            raise AssetError("保存生成媒体必须给出 window_id：没有窗口就无法隔离")

        if mime_type is None:
            mime_type = _FORMAT_MIME.get(fmt.lower())
            if mime_type is None:
                raise AssetError(f"不支持的音频格式：{fmt!r}（允许：{sorted(_FORMAT_MIME)}）")

        # 1. 落盘（原子）。这里抛错时库里什么都没有 —— 正是我们要的。
        stored = self._storage.put(data, expected_digest=expected_digest)

        # 2. 插/改记录。
        existing = await self._session.get(MediaAssetRow, stored.digest)
        if existing is not None:
            if not existing.is_live():
                # 记录存在但不可用（已删除/过期）：**复活它**而不是新建。
                # 主键是 digest，新建会撞主键；而内容确实还在盘上。
                existing.deleted_at = None
                existing.ready_at = utcnow()
                existing.actor_id = actor_id
                existing.window_id = window_id
                if retention_days is not None or resolve_retention_days() > 0:
                    existing.expires_at = _expiry_for(retention_days)
                await self._session.flush()
                return SavedAsset(
                    digest=stored.digest,
                    size_bytes=stored.size_bytes,
                    created=False,
                    relative_path=stored.relative_path,
                )
            # 内容完全一样且仍有效：复用，**不动归属**（见模块文档的坑）。
            logger.debug("生成媒体去重命中：digest=%s", stored.digest[:12])
            return SavedAsset(
                digest=stored.digest,
                size_bytes=stored.size_bytes,
                created=False,
                relative_path=stored.relative_path,
            )

        row = MediaAssetRow(
            digest=stored.digest,
            actor_id=actor_id,
            window_id=window_id,
            subject_id=subject_id,
            kind=kind,
            format=fmt.lower(),
            mime_type=mime_type,
            relative_path=stored.relative_path,
            size_bytes=stored.size_bytes or len(data),
            sample_rate=sample_rate,
            duration_ms=duration_ms,
            turn_id=turn_id,
            message_id=message_id,
            producer=producer,
            # 落盘已经完成，所以这一行出生即 ready。
            ready_at=utcnow(),
            expires_at=_expiry_for(retention_days),
        )
        self._session.add(row)
        await self._session.flush()
        return SavedAsset(
            digest=stored.digest,
            size_bytes=row.size_bytes,
            created=True,
            relative_path=stored.relative_path,
        )

    async def save_stream(
        self,
        chunks: Iterable[bytes],
        **kwargs: object,
    ) -> SavedAsset:
        """流式保存（一次读完再原子落盘）。

        首版不需要真流式落盘：TTS 音频是几百 KB 量级，先聚合到内存再原子写
        换来的是"断流时磁盘上绝无残留文件"这个强保证。真正的分块落盘需要
        边写边算 hash 并在失败时清理，留到有长音频需求时再说。
        """
        payload = b"".join(bytes(chunk) for chunk in chunks)
        return await self.save_bytes(payload, **kwargs)  # type: ignore[arg-type]

    async def save_file(
        self,
        path: Path | str,
        **kwargs: object,
    ) -> SavedAsset:
        """从一个已存在的临时文件保存（TTS 后端直接写盘时用），随后**删除源文件**。

        删除源文件是刻意的：调用方给的是临时产物，留着只会让磁盘上出现
        两份同样的字节，而其中一份没人管。
        """
        source = Path(path)
        raw = source.read_bytes()
        try:
            return await self.save_bytes(raw, **kwargs)  # type: ignore[arg-type]
        finally:
            try:
                source.unlink()
            except OSError:
                logger.debug("无法删除已导入的临时文件：%s", source)

    # -------------------------------------------------------------- 读取

    async def _load_live(self, digest: str, *, now: datetime | None = None) -> MediaAssetRow:
        """取一行**可对外提供**的记录；否则抛 `AssetNotFound`。"""
        row = await self._session.get(MediaAssetRow, digest)
        if row is None or not row.is_live(now=now):
            raise AssetNotFound("媒体不存在或已失效")
        return row

    def _abs_path(self, row: MediaAssetRow) -> Path:
        """记录 → 绝对路径（含越界检查）。"""
        return self._storage.absolute_path_of(row.relative_path)

    async def view(self, digest: str) -> AssetView:
        """不含授权的元数据（调用方负责先确认可见性）。"""
        row = await self._load_live(digest)
        return _to_view(row)

    async def read_for_window(
        self,
        digest: str,
        *,
        requester_actor: str | None,
        requester_window: str | None,
    ) -> bytes:
        """授权后读取字节。**Web 播放走这里**。

        授权规则的唯一实现在 `exports.check_window_access`：这条路与三种
        transport 用的是同一份判定，不会出现"播放能取、渠道不能取"的分叉。
        """
        row = await self._load_live(digest)
        check_window_access(
            requester_window=requester_window,
            requester_actor=requester_actor,
            file_window=row.window_id,
            file_actor=row.actor_id,
        )
        try:
            data = self._storage.read(row.digest)
        except MediaStorageError as exc:
            # 记录在、文件没了 = 存储损坏（不是"没权限"）。要能被发现。
            logger.error("生成媒体记录存在但文件缺失：digest=%s err=%s", row.digest[:12], exc)
            raise AssetNotFound("媒体文件不可用") from exc
        return data

    async def verify_integrity(self, digest: str) -> bool:
        """重算 hash 与记录比对（运维自检用）。"""
        row = await self._session.get(MediaAssetRow, digest)
        if row is None:
            return False
        if not self._storage.exists(row.digest):
            return False
        return hashlib.sha256(self._storage.read(row.digest)).hexdigest() == row.digest

    # -------------------------------------------------------------- 导出

    async def export_for_window(
        self,
        digest: str,
        *,
        transport: ExportTransport,
        requester_actor: str | None,
        requester_window: str | None,
        ttl_seconds: int = DEFAULT_URL_TTL_SECONDS,
    ) -> ExportTarget:
        """授权后按 transport 导出给渠道。

        三种 transport 共用**同一次**授权检查：检查发生在读字节之前、
        transport 分派之前，所以新增 transport 不可能绕过它。
        """
        data = await self.read_for_window(
            digest, requester_actor=requester_actor, requester_window=requester_window
        )
        row = await self._load_live(digest)
        return export_as(
            transport=transport,
            digest=row.digest,
            data=data,
            abs_path=str(self._abs_path(row)),
            mime_type=row.mime_type,
            root_key=self._root_key,
            base_url=self._base_url,
            ttl_seconds=ttl_seconds,
        )

    async def sign_url_for_window(
        self,
        digest: str,
        *,
        requester_actor: str | None,
        requester_window: str | None,
        ttl_seconds: int = DEFAULT_URL_TTL_SECONDS,
    ) -> ExportTarget:
        """只签发 URL（不读文件内容，省一次 IO）。"""
        target = await self.export_for_window(
            digest,
            transport=ExportTransport.signed_url,
            requester_actor=requester_actor,
            requester_window=requester_window,
            ttl_seconds=ttl_seconds,
        )
        return target

    async def resolve_signed(
        self, digest: str, *, expires_at: int, signature: str, now: float | None = None
    ) -> bytes:
        """**签名 URL 的落地点**：凭签名取字节，不需要登录态。

        签名本身即授权（见 `exports.py`），所以这里**不**再查窗口归属——
        签名的目标就是让没有 cookie 的渠道也能取。反过来说，签名一旦泄漏，
        在有效期内任何人都能取，这是刻意的取舍：有效期只有几分钟。

        仍然要确认记录**当前**可用：签发后文件可能已被清理，那种情况要给
        404 而不是把旧字节交出去。
        """
        from ling.domains.media.exports import verify_file_url

        verify_file_url(
            root_key=self._root_key,
            file_id=digest,
            expires_at=expires_at,
            signature=signature,
            now=now,
        )
        row = await self._load_live(digest)
        try:
            return self._storage.read(row.digest)
        except MediaStorageError as exc:
            raise AssetNotFound("媒体文件不可用") from exc

    # -------------------------------------------------------------- 删除与过期

    async def soft_delete(self, digest: str, *, reason: str = "") -> bool:
        """标记删除。**立即对所有读取路径不可见**，文件由清理任务稍后删。"""
        row = await self._session.get(MediaAssetRow, digest)
        if row is None or row.deleted_at is not None:
            return False
        row.deleted_at = utcnow()
        row.note = reason or row.note
        await self._session.flush()
        return True

    async def purge_expired(
        self,
        *,
        now: datetime | None = None,
        grace_seconds: int = DEFAULT_DELETE_GRACE_SECONDS,
        limit: int = 200,
    ) -> int:
        """清理过期资产，返回真正删掉的文件数。

        两趟：

        1. 把 `expires_at <= now` 的**未删除**记录标成软删（此刻起不可见）；
        2. 把软删已经超过 `grace_seconds` 的记录连文件一起删。

        两趟分开是为了让"不可见"立刻生效、"删文件"延后——在途任务与已签发
        的 URL 因此不会被半路抽走文件。
        """
        current = now or utcnow()
        purged = 0

        expired = (
            await self._session.execute(
                select(MediaAssetRow)
                .where(
                    MediaAssetRow.deleted_at.is_(None),
                    MediaAssetRow.expires_at.is_not(None),
                    MediaAssetRow.expires_at <= current,
                )
                .limit(limit)
            )
        ).scalars()
        marked = 0
        for row in expired:
            row.deleted_at = current
            marked += 1
        if marked:
            await self._session.flush()

        cutoff = current - timedelta(seconds=grace_seconds)
        candidates = (
            await self._session.execute(
                select(MediaAssetRow)
                .where(
                    MediaAssetRow.deleted_at.is_not(None),
                    MediaAssetRow.deleted_at <= cutoff,
                )
                .limit(limit)
            )
        ).scalars()
        for row in candidates:
            if await self._still_referenced(row.digest):
                # 还有在途任务/证据引用它：只保留记录，不删文件，下一轮再看。
                continue
            if self._storage.delete(row.digest):
                purged += 1
            await self._session.delete(row)
        if purged:
            await self._session.flush()

        if marked or purged:
            logger.info("生成媒体清理：标记 %d 条过期，删除 %d 个文件", marked, purged)
        return purged

    async def _still_referenced(self, digest: str) -> bool:
        """文件是否还被别处引用（目前只有 `media_evidence.media_ref`）。"""
        referenced = (
            await self._session.execute(
                select(MediaEvidenceRow.id).where(MediaEvidenceRow.media_ref == digest).limit(1)
            )
        ).scalar_one_or_none()
        return referenced is not None

    async def collect_orphans(self, *, limit: int = 500) -> int:
        """删掉**没有任何记录指向**的磁盘文件，返回删除个数。

        孤儿是"先落盘后写库"的正常副产物（两步之间进程被杀）。它们无害但占
        空间。只清理超过宽限期的文件，避免删掉别的进程正在保存的临时产物。
        """
        rows = (await self._session.execute(select(MediaAssetRow.digest))).scalars().all()
        known = set(rows)
        removed = 0
        root = self._storage.root
        if not root.is_dir():
            return 0
        for shard in sorted(root.iterdir()):
            if not shard.is_dir():
                continue
            for candidate in shard.iterdir():
                if candidate.name.startswith("."):
                    continue
                if candidate.stem not in known:
                    try:
                        candidate.unlink()
                        removed += 1
                    except OSError:
                        continue
                if removed >= limit:
                    return removed
        return removed


def _expiry_for(retention_days: int | None) -> datetime | None:
    days = resolve_retention_days(retention_days)
    if days <= 0:
        return None
    return utcnow() + timedelta(days=days)


def _to_view(row: MediaAssetRow) -> AssetView:
    return AssetView(
        digest=row.digest,
        kind=row.kind,
        format=row.format,
        mime_type=row.mime_type,
        size_bytes=row.size_bytes,
        sample_rate=row.sample_rate,
        duration_ms=row.duration_ms,
        created_at=row.created_at,
        expires_at=row.expires_at,
    )


async def list_window_assets(
    session: AsyncSession,
    *,
    actor_id: str,
    window_id: str,
    limit: int = 50,
    now: datetime | None = None,
) -> list[AssetView]:
    """列出某窗口里可播放的产物（**只返回仍有效的**）。"""
    current = now or utcnow()
    rows: Sequence[MediaAssetRow] = (
        (
            await session.execute(
                select(MediaAssetRow)
                .where(
                    MediaAssetRow.actor_id == actor_id,
                    MediaAssetRow.window_id == window_id,
                    MediaAssetRow.deleted_at.is_(None),
                    MediaAssetRow.ready_at.is_not(None),
                )
                .order_by(MediaAssetRow.created_at.desc())
                .limit(limit)
            )
        )
        .scalars()
        .all()
    )
    return [_to_view(row) for row in rows if row.is_live(now=current)]


__all__ = [
    "AssetError",
    "AssetNotFound",
    "AssetView",
    "ExportDenied",
    "ExportTransport",
    "GeneratedMediaService",
    "SavedAsset",
    "list_window_assets",
    "resolve_retention_days",
]
