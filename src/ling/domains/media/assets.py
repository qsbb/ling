"""受管媒体的保存、窗口授权、租约与清理。

内容按 digest 存一份，逻辑资产按 asset_id 隔离窗口、用途和期限。
存量 digest 引用继续对应原始归属；新调用使用不透明 asset_id。
保存与清理持有同一内容锁，GC 只删除已无引用或租约的内容。
"""

from __future__ import annotations

import hashlib
import logging
import os
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from sqlalchemy import cast, delete, exists, or_, select, text
from sqlalchemy.dialects.postgresql import JSONB
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
from ling.infra.media_assets import MediaAssetGrantRow, MediaAssetLeaseRow, MediaAssetRow, utcnow
from ling.infra.models import MediaEvidenceRow
from ling.infra.security import new_id

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
    "webm": "audio/webm",
    "flac": "audio/flac",
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
    digest: str
    size_bytes: int
    created: bool
    relative_path: str
    asset_id: str = ""


@dataclass(frozen=True)
class AssetView:
    """授权后的逻辑资产元数据，包含真实内容摘要。"""

    digest: str
    kind: str
    format: str
    mime_type: str
    size_bytes: int
    sample_rate: int | None
    duration_ms: int | None
    created_at: datetime
    expires_at: datetime | None
    asset_id: str = ""
    actor_id: str = ""
    window_id: str = ""
    subject_id: str | None = None
    purpose: str = "tts_output"
    message_id: str | None = None
    relative_path: str = ""


async def load_asset(
    session: AsyncSession, reference: str, *, now: datetime | None = None
) -> AssetView:
    """解析逻辑 ID 或旧 digest。归属授权由调用方执行。"""
    grant = await session.get(MediaAssetGrantRow, reference, populate_existing=True)
    digest = grant.blob_digest if grant is not None else reference
    await session.execute(
        text("SELECT pg_advisory_xact_lock(hashtextextended(:asset_key, 0))"),
        {"asset_key": f"media_blob:{digest}"},
    )
    if grant is not None:
        await session.refresh(grant)
        blob = await session.get(MediaAssetRow, grant.blob_digest, populate_existing=True)
        if blob is None or blob.ready_at is None or not grant.is_live(now=now):
            raise AssetNotFound("媒体不存在或已失效")
        return _to_view(blob, grant)
    blob = await session.get(MediaAssetRow, reference, populate_existing=True)
    if blob is None or not blob.is_live(now=now):
        raise AssetNotFound("媒体不存在或已失效")
    return _to_view(blob)


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
        purpose: str = "tts_output",
        retention_seconds: int | None = None,
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

        digest = hashlib.sha256(data).hexdigest()
        await self._lock_blob(digest)
        stored = self._storage.put(data, expected_digest=expected_digest)
        blob = await self._session.get(MediaAssetRow, stored.digest, populate_existing=True)
        created = blob is None
        expiry = (
            utcnow() + timedelta(seconds=retention_seconds)
            if retention_seconds is not None
            else _expiry_for(retention_days)
        )
        if blob is None:
            blob = MediaAssetRow(
                digest=stored.digest,
                actor_id=actor_id,
                window_id=window_id,
                subject_id=subject_id,
                kind=kind,
                format=fmt.lower(),
                mime_type=mime_type,
                relative_path=stored.relative_path,
                size_bytes=stored.size_bytes,
                sample_rate=sample_rate,
                duration_ms=duration_ms,
                turn_id=turn_id,
                message_id=message_id,
                producer=producer,
                ready_at=utcnow(),
                expires_at=expiry,
            )
            self._session.add(blob)
            await self._session.flush()


        grant = await self._session.scalar(
            select(MediaAssetGrantRow)
            .where(
                MediaAssetGrantRow.blob_digest == stored.digest,
                MediaAssetGrantRow.actor_id == actor_id,
                MediaAssetGrantRow.window_id == window_id,
                MediaAssetGrantRow.purpose == purpose,
                MediaAssetGrantRow.message_id == message_id,
                MediaAssetGrantRow.deleted_at.is_(None),
            )
            .order_by(MediaAssetGrantRow.created_at.desc())
            .limit(1)
        )
        if grant is None or not grant.is_live():
            grant = MediaAssetGrantRow(
                asset_id=new_id("asset"),
                blob_digest=stored.digest,
                actor_id=actor_id,
                window_id=window_id,
                subject_id=subject_id,
                purpose=purpose,
                turn_id=turn_id,
                message_id=message_id,
                producer=producer,
                expires_at=expiry,
            )
            self._session.add(grant)
        else:
            grant.expires_at = expiry
        await self._session.flush()
        return SavedAsset(
            digest=stored.digest,
            size_bytes=stored.size_bytes,
            created=created,
            relative_path=stored.relative_path,
            asset_id=grant.asset_id,
        )

    async def _lock_blob(self, digest: str) -> None:
        await self._session.execute(
            text("SELECT pg_advisory_xact_lock(hashtextextended(:asset_key, 0))"),
            {"asset_key": f"media_blob:{digest}"},
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

    async def _load_live(self, reference: str, *, now: datetime | None = None) -> AssetView:
        return await load_asset(self._session, reference, now=now)

    def _abs_path(self, row: AssetView) -> Path:
        return self._storage.absolute_path_of(row.relative_path)

    async def view(self, reference: str) -> AssetView:
        return await self._load_live(reference)

    async def view_for_window(
        self, reference: str, *, requester_actor: str | None, requester_window: str | None
    ) -> AssetView:
        row = await self._load_live(reference)
        check_window_access(
            requester_window=requester_window,
            requester_actor=requester_actor,
            file_window=row.window_id,
            file_actor=row.actor_id,
        )
        return row

    async def acquire_lease(
        self,
        reference: str,
        *,
        requester_actor: str | None,
        requester_window: str | None,
        ttl_seconds: int = 180,
    ) -> str:
        if ttl_seconds <= 0:
            raise AssetError("租约期限必须为正")
        row = await self.view_for_window(
            reference, requester_actor=requester_actor, requester_window=requester_window
        )
        await self._lock_blob(row.digest)
        row = await self.view_for_window(
            reference, requester_actor=requester_actor, requester_window=requester_window
        )
        asset_id = row.asset_id
        if not asset_id:
            # 存量 digest 的授权主体确定，按原归属补建可租赁逻辑引用。
            grant = MediaAssetGrantRow(
                asset_id=new_id("asset"),
                blob_digest=row.digest,
                actor_id=row.actor_id,
                window_id=row.window_id,
                subject_id=row.subject_id,
                purpose=row.purpose,
                expires_at=row.expires_at,
            )
            self._session.add(grant)
            await self._session.flush()
            asset_id = grant.asset_id
        lease_id = new_id("lease")
        self._session.add(
            MediaAssetLeaseRow(
                lease_id=lease_id,
                asset_id=asset_id,
                expires_at=utcnow() + timedelta(seconds=ttl_seconds),
            )
        )
        await self._session.flush()
        return lease_id

    async def release_lease(self, lease_id: str) -> None:
        await self._session.execute(
            delete(MediaAssetLeaseRow).where(MediaAssetLeaseRow.lease_id == lease_id)
        )

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
        row = await self.view_for_window(
            digest, requester_actor=requester_actor, requester_window=requester_window
        )
        await self._lock_blob(row.digest)
        row = await self.view_for_window(
            digest, requester_actor=requester_actor, requester_window=requester_window
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
        try:
            row = await self._load_live(digest)
        except AssetNotFound:
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
        if transport in (ExportTransport.local_path, ExportTransport.signed_url):
            await self.acquire_lease(
                digest,
                requester_actor=requester_actor,
                requester_window=requester_window,
                ttl_seconds=ttl_seconds,
            )
        return export_as(
            transport=transport,
            digest=digest,
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

    async def soft_delete(self, reference: str, *, reason: str = "") -> bool:
        grant = await self._session.get(MediaAssetGrantRow, reference, populate_existing=True)
        if grant is not None:
            if grant.deleted_at is not None:
                return False
            grant.deleted_at = utcnow()
            grant.note = reason or grant.note
            blob = await self._session.get(MediaAssetRow, grant.blob_digest)
            if blob is not None and (blob.actor_id, blob.window_id) == (
                grant.actor_id,
                grant.window_id,
            ):
                blob.deleted_at = utcnow()
            await self._session.flush()
            return True
        blob = await self._session.get(MediaAssetRow, reference, populate_existing=True)
        if blob is None or blob.deleted_at is not None:
            return False
        blob.deleted_at = utcnow()
        blob.note = reason or blob.note
        # 存量 digest 删除只撤销原归属的引用，其他窗口引用独立存活。
        grants = (
            await self._session.scalars(
                select(MediaAssetGrantRow).where(
                    MediaAssetGrantRow.blob_digest == blob.digest,
                    MediaAssetGrantRow.actor_id == blob.actor_id,
                    MediaAssetGrantRow.window_id == blob.window_id,
                )
            )
        ).all()
        for item in grants:
            item.deleted_at = utcnow()
        await self._session.flush()
        return True

    async def purge_expired(
        self,
        *,
        now: datetime | None = None,
        grace_seconds: int = DEFAULT_DELETE_GRACE_SECONDS,
        limit: int = 200,
    ) -> int:
        current = now or utcnow()
        cutoff = current - timedelta(seconds=grace_seconds)
        await self._session.execute(
            delete(MediaAssetLeaseRow).where(MediaAssetLeaseRow.expires_at <= current)
        )
        grant_expired = exists(
            select(MediaAssetGrantRow.asset_id).where(
                MediaAssetGrantRow.blob_digest == MediaAssetRow.digest,
                or_(
                    MediaAssetGrantRow.deleted_at.is_not(None),
                    MediaAssetGrantRow.expires_at <= current,
                ),
            )
        )
        blobs = (
            await self._session.scalars(
                select(MediaAssetRow)
                .where(
                    or_(
                        MediaAssetRow.deleted_at.is_not(None),
                        MediaAssetRow.expires_at <= current,
                        grant_expired,
                    )
                )
                .order_by(MediaAssetRow.created_at)
                .limit(limit)
            )
        ).all()
        purged = 0
        for blob in blobs:
            await self._lock_blob(blob.digest)
            await self._session.refresh(blob)
            grants = (
                await self._session.scalars(
                    select(MediaAssetGrantRow)
                    .where(MediaAssetGrantRow.blob_digest == blob.digest)
                    .execution_options(populate_existing=True)
                )
            ).all()
            for grant in grants:
                if (
                    grant.deleted_at is None
                    and grant.expires_at is not None
                    and grant.expires_at <= current
                ):
                    grant.deleted_at = current
            if (
                blob.deleted_at is None
                and blob.expires_at is not None
                and blob.expires_at <= current
            ):
                blob.deleted_at = current
            await self._session.flush()
            if blob.deleted_at is None or blob.deleted_at > cutoff:
                continue
            if any(g.deleted_at is None or g.deleted_at > cutoff for g in grants):
                continue
            leased = await self._session.scalar(
                select(MediaAssetLeaseRow.lease_id)
                .join(
                    MediaAssetGrantRow, MediaAssetLeaseRow.asset_id == MediaAssetGrantRow.asset_id
                )
                .where(
                    MediaAssetGrantRow.blob_digest == blob.digest,
                    MediaAssetLeaseRow.expires_at > current,
                )
                .limit(1)
            )
            if leased is not None or await self._still_referenced(blob.digest, grants):
                continue
            for grant in grants:
                await self._session.delete(grant)
            await self._session.flush()
            if self._storage.delete(blob.digest):
                purged += 1
            await self._session.delete(blob)
        await self._session.flush()
        return purged

    async def _still_referenced(
        self, digest: str, grants: Sequence[MediaAssetGrantRow] = ()
    ) -> bool:
        refs = [digest, f"asset:{digest}"]
        for grant in grants:
            refs.extend((grant.asset_id, f"asset:{grant.asset_id}"))
        if (
            await self._session.scalar(
                select(MediaEvidenceRow.id).where(MediaEvidenceRow.media_ref.in_(refs)).limit(1)
            )
            is not None
        ):
            return True
        if not grants:
            return False
        from ling.infra.asr_jobs import AsrJob

        payload_matches = [
            cast(AsrJob.payload, JSONB).contains({"audio": [{"asset_id": grant.asset_id}]})
            for grant in grants
        ]
        return (
            await self._session.scalar(
                select(AsrJob.id)
                .where(
                    AsrJob.state.in_(("queued", "running", "succeeded", "unknown")),
                    AsrJob.submission == "pending",
                    or_(*payload_matches),
                )
                .limit(1)
            )
            is not None
        )

    async def collect_orphans(self, *, limit: int = 500) -> int:
        """持内容锁复查数据库，避免清理并发保存中的内容。"""
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
                await self._lock_blob(candidate.name)
                exists = await self._session.get(
                    MediaAssetRow, candidate.name, populate_existing=True
                )
                if exists is None:
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


def _to_view(blob: MediaAssetRow, grant: MediaAssetGrantRow | None = None) -> AssetView:
    owner = grant or blob
    return AssetView(
        digest=blob.digest,
        kind=blob.kind,
        format=blob.format,
        mime_type=blob.mime_type,
        size_bytes=blob.size_bytes,
        sample_rate=blob.sample_rate,
        duration_ms=blob.duration_ms,
        created_at=owner.created_at,
        expires_at=owner.expires_at,
        asset_id=grant.asset_id if grant else "",
        actor_id=owner.actor_id,
        window_id=owner.window_id,
        subject_id=owner.subject_id,
        purpose=grant.purpose if grant else "tts_output",
        message_id=owner.message_id,
        relative_path=blob.relative_path,
    )


async def list_window_assets(
    session: AsyncSession,
    *,
    actor_id: str,
    window_id: str,
    limit: int = 50,
    now: datetime | None = None,
) -> list[AssetView]:
    current = now or utcnow()
    rows = (
        await session.execute(
            select(MediaAssetRow, MediaAssetGrantRow)
            .join(MediaAssetGrantRow, MediaAssetGrantRow.blob_digest == MediaAssetRow.digest)
            .where(
                MediaAssetGrantRow.actor_id == actor_id,
                MediaAssetGrantRow.window_id == window_id,
                MediaAssetGrantRow.deleted_at.is_(None),
                MediaAssetRow.ready_at.is_not(None),
            )
            .order_by(MediaAssetGrantRow.created_at.desc())
            .limit(limit)
        )
    ).all()
    views = [_to_view(blob, grant) for blob, grant in rows if grant.is_live(now=current)]
    known = {view.digest for view in views}
    legacy = (
        await session.scalars(
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
    ).all()
    views.extend(
        _to_view(row) for row in legacy if row.digest not in known and row.is_live(now=current)
    )
    return views[:limit]


__all__ = [
    "AssetError",
    "AssetNotFound",
    "AssetView",
    "ExportDenied",
    "ExportTransport",
    "GeneratedMediaService",
    "SavedAsset",
    "list_window_assets",
    "load_asset",
    "resolve_retention_days",
]
