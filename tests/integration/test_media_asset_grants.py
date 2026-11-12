"""逻辑资产共享内容时仍分别授权、撤销和清理。"""

from datetime import timedelta

import pytest
from sqlalchemy import select

from ling.domains.media.assets import AssetNotFound, GeneratedMediaService, list_window_assets
from ling.domains.media.exports import ExportDenied, ExportTransport
from ling.domains.media.storage import GeneratedMediaStorage
from ling.infra.db import session_scope
from ling.infra.media_assets import MediaAssetGrantRow, MediaAssetLeaseRow, MediaAssetRow, utcnow

pytestmark = pytest.mark.w0_web
AUDIO = b"fixture-shared-audio-content"
ACTOR = "actor_grants"
WINDOW_A = "web:grant-a"
WINDOW_B = "web:grant-b"
KEY = b"k" * 32


def service(session, tmp_path):  # noqa: ANN001, ANN201
    return GeneratedMediaService(session, data_dir=tmp_path, root_key=KEY)


async def save_pair(tmp_path):  # noqa: ANN001, ANN201
    async with session_scope() as session:
        media = service(session, tmp_path)
        first = await media.save_bytes(AUDIO, actor_id=ACTOR, window_id=WINDOW_A)
        second = await media.save_bytes(AUDIO, actor_id=ACTOR, window_id=WINDOW_B)
        return first, second


async def test_identical_content_has_independent_authorized_assets(web_env, tmp_path):  # noqa: ANN001, ANN201
    first, second = await save_pair(tmp_path)
    assert first.digest == second.digest
    assert first.asset_id != second.asset_id
    assert first.asset_id.startswith("asset_")
    async with session_scope() as session:
        media = service(session, tmp_path)
        for saved, window in ((first, WINDOW_A), (second, WINDOW_B)):
            assert await media.read_for_window(
                saved.asset_id, requester_actor=ACTOR, requester_window=window
            ) == AUDIO
        with pytest.raises(ExportDenied):
            await media.read_for_window(
                first.asset_id, requester_actor=ACTOR, requester_window=WINDOW_B
            )
        # 旧内容摘要永远对应原始归属。
        assert await media.read_for_window(
            first.digest, requester_actor=ACTOR, requester_window=WINDOW_A
        ) == AUDIO
        with pytest.raises(ExportDenied):
            await media.read_for_window(
                first.digest, requester_actor=ACTOR, requester_window=WINDOW_B
            )
        assert len((await session.scalars(select(MediaAssetRow))).all()) == 1
        assert len((await session.scalars(select(MediaAssetGrantRow))).all()) == 2


async def test_delete_one_asset_keeps_other_window_readable(web_env, tmp_path):  # noqa: ANN001, ANN201
    first, second = await save_pair(tmp_path)
    async with session_scope() as session:
        media = service(session, tmp_path)
        assert await media.soft_delete(first.asset_id)
        assert await media.purge_expired(grace_seconds=0) == 0
        with pytest.raises(AssetNotFound):
            await media.read_for_window(
                first.asset_id, requester_actor=ACTOR, requester_window=WINDOW_A
            )
        with pytest.raises(AssetNotFound):
            await media.read_for_window(
                first.digest, requester_actor=ACTOR, requester_window=WINDOW_A
            )
        assert await media.read_for_window(
            second.asset_id, requester_actor=ACTOR, requester_window=WINDOW_B
        ) == AUDIO


async def test_lease_retains_content_until_its_owner_releases(web_env, tmp_path):  # noqa: ANN001, ANN201
    first, second = await save_pair(tmp_path)
    async with session_scope() as session:
        media = service(session, tmp_path)
        lease = await media.acquire_lease(
            second.asset_id, requester_actor=ACTOR, requester_window=WINDOW_B
        )
        another_lease = await media.acquire_lease(
            second.asset_id, requester_actor=ACTOR, requester_window=WINDOW_B
        )
        await media.soft_delete(first.asset_id)
        await media.soft_delete(second.asset_id)
        await media.release_lease(lease)
        assert await media.purge_expired(grace_seconds=0) == 0
        assert GeneratedMediaStorage(tmp_path).exists(first.digest)
        await media.release_lease(another_lease)
        assert await media.purge_expired(grace_seconds=0) == 1
        assert not GeneratedMediaStorage(tmp_path).exists(first.digest)


async def test_expired_lease_does_not_prevent_collection(web_env, tmp_path):  # noqa: ANN001, ANN201
    first, second = await save_pair(tmp_path)
    async with session_scope() as session:
        media = service(session, tmp_path)
        lease_id = await media.acquire_lease(
            first.asset_id, requester_actor=ACTOR, requester_window=WINDOW_A
        )
        lease = await session.get(MediaAssetLeaseRow, lease_id)
        assert lease is not None
        lease.expires_at = utcnow() - timedelta(seconds=1)
        await media.soft_delete(first.asset_id)
        await media.soft_delete(second.asset_id)
        assert await media.purge_expired(grace_seconds=0) == 1


async def test_asset_purposes_have_independent_retention(web_env, tmp_path):  # noqa: ANN001, ANN201
    async with session_scope() as session:
        media = service(session, tmp_path)
        output = await media.save_bytes(
            AUDIO, actor_id=ACTOR, window_id=WINDOW_A, purpose="tts_output", retention_days=1
        )
        reference = await media.save_bytes(
            AUDIO, actor_id=ACTOR, window_id=WINDOW_A, purpose="tts_reference", retention_days=0
        )
        assert output.asset_id != reference.asset_id
        before = await media.view(reference.asset_id)
        assert before.expires_at is None
        await media.soft_delete(output.asset_id)
        assert await media.purge_expired(grace_seconds=0) == 0
        assert await media.read_for_window(
            reference.asset_id, requester_actor=ACTOR, requester_window=WINDOW_A
        ) == AUDIO


async def test_expired_asset_is_not_resurrected_by_new_save(web_env, tmp_path):  # noqa: ANN001, ANN201
    async with session_scope() as session:
        media = service(session, tmp_path)
        old = await media.save_bytes(AUDIO, actor_id=ACTOR, window_id=WINDOW_A)
        grant = await session.get(MediaAssetGrantRow, old.asset_id)
        assert grant is not None
        grant.expires_at = utcnow() - timedelta(seconds=1)
        new = await media.save_bytes(AUDIO, actor_id=ACTOR, window_id=WINDOW_A)
        assert old.asset_id != new.asset_id
        with pytest.raises(AssetNotFound):
            await media.view(old.asset_id)
        assert await media.read_for_window(
            new.asset_id, requester_actor=ACTOR, requester_window=WINDOW_A
        ) == AUDIO
        assets = await list_window_assets(session, actor_id=ACTOR, window_id=WINDOW_A)
        assert [item.asset_id for item in assets] == [new.asset_id]


async def test_logical_signed_url_stops_working_after_revocation(web_env, tmp_path):  # noqa: ANN001, ANN201
    from urllib.parse import parse_qs, urlparse

    first, second = await save_pair(tmp_path)
    async with session_scope() as session:
        media = service(session, tmp_path)
        exported = await media.export_for_window(
            second.asset_id, transport=ExportTransport.signed_url,
            requester_actor=ACTOR, requester_window=WINDOW_B,
        )
        assert exported.url is not None and second.asset_id in exported.url
        query = parse_qs(urlparse(exported.url).query)
        kwargs = {"expires_at": int(query["exp"][0]), "signature": query["sig"][0]}
        assert await media.resolve_signed(second.asset_id, **kwargs) == AUDIO
        await media.soft_delete(second.asset_id)
        with pytest.raises(AssetNotFound):
            await media.resolve_signed(second.asset_id, **kwargs)
        assert await media.read_for_window(
            first.asset_id, requester_actor=ACTOR, requester_window=WINDOW_A
        ) == AUDIO
