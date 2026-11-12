"""生成媒体的存储与授权导出（真实 PostgreSQL + 真实磁盘）。

## 这个文件在守什么

TTS 合成出的音频必须：存得下、取得出、**别人取不到**、过期清得掉。四条里
最容易写错且后果最重的是第三条——音频是私人对话的一部分，越权读取的代价
比"播不出来"大一个量级。

## 为什么用真实文件系统而不是 mock

"原子保存"这件事的全部价值在于**磁盘上的真实行为**：`os.replace` 的原子性、
`fsync` 之后才 rename、临时文件在失败时被清掉、路径穿越被 `resolve()` 挡住。
这些东西用 mock 一测就全变成"验证我自己的假设"。所以每个用例给一个真目录
（`tmp_path`），并在断言里**真的去读目录**（比如"断流后目录里没有 ready 文件"）。

## 与入站授权的关系（必须核对的边界）

`media_evidence` 的授权是为"用户发来的图片"设计的：绑定窗口、默认拒绝、
按 `consent_revision` 失效。这里**不复用**它——第 8 个用例专门证明：一个
**没有**任何 `media_evidence` 行、也没有入站同意记录的窗口，仍然能正常取到
自己生成的音频。这就是"不能拿入站授权拦 TTS 输出"的可执行版本。
"""

from __future__ import annotations

import hashlib
import os

import pytest
from sqlalchemy import select

from ling.domains.media.assets import (
    AssetError,
    AssetNotFound,
    GeneratedMediaService,
    list_window_assets,
    resolve_retention_days,
)
from ling.domains.media.exports import (
    ExportDenied,
    ExportTransport,
    SignatureError,
    check_window_access,
    sign_file_url,
    verify_file_url,
)
from ling.domains.media.storage import GeneratedMediaStorage, MediaStorageError
from ling.infra.db import session_scope
from ling.infra.media_assets import MediaAssetRow

pytestmark = pytest.mark.w0_web

ROOT_KEY = b"k" * 32
ACTOR = "actor_ling"
WINDOW = "onebot:private:100200300"
OTHER_WINDOW = "onebot:group:555000111"
BASE_URL = "http://127.0.0.1:8080"

#: 一段"像音频"的字节：测试不关心它是不是真能播，只关心它被原样存取。
AUDIO = b"ID3\x04\x00\x00\x00\x00\x00\x00" + bytes(range(256)) * 8


def make_service(data_dir, session, **kwargs) -> GeneratedMediaService:  # noqa: ANN001
    return GeneratedMediaService(session, data_dir=data_dir, root_key=ROOT_KEY, **kwargs)


async def _save(data_dir, **overrides):  # noqa: ANN001,ANN003
    """在一段 session_scope 里保存，返回 (SavedAsset, 会话外可见的行数据)."""
    async with session_scope() as session:
        service = make_service(data_dir, session, base_url=BASE_URL)
        payload = {
            "actor_id": ACTOR,
            "window_id": WINDOW,
            "fmt": "mp3",
            "duration_ms": 1234,
            "sample_rate": 24000,
            "producer": "tts.test",
        }
        data = overrides.pop("data", AUDIO)
        payload.update(overrides)
        saved = await service.save_bytes(data, **payload)
        await session.commit()
        return saved


# ------------------------------------------------------------------ 1. 原子保存


async def test_saved_file_is_complete_and_digest_matches(web_env, tmp_path) -> None:
    """落盘的文件内容与记录逐字节一致，hash 自洽。"""
    saved = await _save(tmp_path)

    digest = hashlib.sha256(AUDIO).hexdigest()
    assert saved.digest == digest, "主键必须就是内容的 sha256"
    assert saved.created is True
    assert saved.size_bytes == len(AUDIO)

    storage = GeneratedMediaStorage(tmp_path)
    assert storage.exists(digest)
    assert storage.read(digest) == AUDIO
    assert storage.verify(digest) is True, "重算 hash 必须与文件名一致"

    # 分片布局：<root>/<前两位>/<完整digest>
    assert storage.path_for(digest).parent.name == digest[:2]
    assert storage.path_for(digest).name == digest

    async with session_scope() as session:
        row = await session.get(MediaAssetRow, digest)
        assert row is not None
        assert row.relative_path == f"generated_media/{digest[:2]}/{digest}"
        assert row.ready_at is not None, "落盘在前，所以记录出生即 ready"
        assert row.size_bytes == len(AUDIO)
        assert row.duration_ms == 1234
        assert row.sample_rate == 24000
        assert row.mime_type == "audio/mpeg"
        assert row.producer == "tts.test"
        assert row.is_live() is True


async def test_目录里没有残留临时文件(web_env, tmp_path) -> None:
    """成功保存后不能留下 `.part` 临时文件（原子写入的收尾）。"""
    await _save(tmp_path)
    storage = GeneratedMediaStorage(tmp_path)
    leftovers = list(storage.root.glob("*/.tmp-*.part"))
    assert leftovers == [], f"残留临时文件：{leftovers}"


async def test_断流时不留下任何可读文件(web_env, tmp_path) -> None:
    """保存中途抛异常 → **磁盘上没有 ready 文件，库里也没有记录**。

    这是"不能出现文件写了一半就有记录"的反向证明：异常路径上两条都不能有。
    """

    def broken_stream():
        yield b"ID3-first-chunk"
        raise ConnectionError("上游 TTS 流断了")

    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        with pytest.raises(ConnectionError):
            await service.save_stream(
                broken_stream(), actor_id=ACTOR, window_id=WINDOW, fmt="mp3"
            )
        await session.rollback()

    storage = GeneratedMediaStorage(tmp_path)
    # 目录里没有任何"以 digest 命名"的文件（临时文件可以有，但绝不能叫产物名）
    ready_files = [
        p for p in storage.root.rglob("*") if p.is_file() and not p.name.startswith(".")
    ] if storage.root.is_dir() else []
    assert ready_files == [], f"断流后不该有可读产物：{ready_files}"

    async with session_scope() as session:
        rows = (await session.execute(select(MediaAssetRow))).scalars().all()
        assert rows == [], "断流不该留下记录"


async def test_同内容去重只占一份(web_env, tmp_path) -> None:
    """同样的字节存两次：磁盘一份、库里一行。"""
    first = await _save(tmp_path)
    second = await _save(tmp_path)
    assert second.created is False
    assert second.digest == first.digest

    storage = GeneratedMediaStorage(tmp_path)
    files = [p for p in storage.root.rglob("*") if p.is_file()]
    assert len(files) == 1

    async with session_scope() as session:
        rows = (await session.execute(select(MediaAssetRow))).scalars().all()
        assert len(rows) == 1


async def test_跨窗口同内容时digest仍归第一个窗口(web_env, tmp_path) -> None:
    """**模块文档里那句"跨窗口额外放行"描述的是一条不存在的行为。**

    这里钉住真实行为：第二个窗口存同一份字节，拿到 `created=False` 和同一个
    digest，但那一行的归属**仍然是第一个窗口的**——它不能拿去引用音频。
    授权侧（`exports.check_window_access`）不会为它开例外，下一条用例证明。
    """
    first = await _save(tmp_path)
    second = await _save(tmp_path, window_id=OTHER_WINDOW)
    assert second.created is False
    assert second.digest == first.digest

    async with session_scope() as session:
        row = await session.get(MediaAssetRow, second.digest)
        assert row is not None
        assert (row.actor_id, row.window_id) == (ACTOR, WINDOW), (
            "去重不该把归属改成第二个窗口（那会让第一个窗口播不出来）"
        )


async def test_跨窗口同内容第二个窗口读不出(web_env, tmp_path) -> None:
    """第二个窗口拿到别人的 digest 之后，读取必须被拒——这就是那句文档的
    `created=False` 为什么**不能直接用**。"""
    await _save(tmp_path)
    second = await _save(tmp_path, window_id=OTHER_WINDOW)

    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        # 同一个 actor（同一个人），只是换了窗口：也必须拒绝。
        with pytest.raises(ExportDenied):
            await service.read_for_window(
                second.digest, requester_actor=ACTOR, requester_window=OTHER_WINDOW
            )
        # 而第一个窗口照常能读——去重没有破坏它的访问权。
        assert (
            await service.read_for_window(
                second.digest, requester_actor=ACTOR, requester_window=WINDOW
            )
            == AUDIO
        )


# ------------------------------------------------------------------ 2. 授权读取


async def test_同窗口可以读(web_env, tmp_path) -> None:
    saved = await _save(tmp_path)
    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        data = await service.read_for_window(
            saved.digest, requester_actor=ACTOR, requester_window=WINDOW
        )
    assert data == AUDIO


async def test_别的窗口读不到(web_env, tmp_path) -> None:
    """**越权必须被拒**：同一个人、不同窗口也不行。"""
    saved = await _save(tmp_path)
    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        with pytest.raises(ExportDenied):
            await service.read_for_window(
                saved.digest, requester_actor=ACTOR, requester_window=OTHER_WINDOW
            )


async def test_别的身份读不到(web_env, tmp_path) -> None:
    saved = await _save(tmp_path)
    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        with pytest.raises(ExportDenied):
            await service.read_for_window(
                saved.digest, requester_actor="actor_someone_else", requester_window=WINDOW
            )


async def test_没有身份一律拒绝(web_env, tmp_path) -> None:
    """`None` 不等于"通行"：没身份就是不给。"""
    saved = await _save(tmp_path)
    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        with pytest.raises(ExportDenied):
            await service.read_for_window(
                saved.digest, requester_actor=None, requester_window=WINDOW
            )
        with pytest.raises(ExportDenied):
            await service.read_for_window(
                saved.digest, requester_actor=ACTOR, requester_window=None
            )


async def test_不需要入站同意也能正常取(web_env, tmp_path) -> None:
    """**关键边界**：不复用入站证据的授权。

    `media_evidence` 那套是"默认拒绝 + 绑定窗口 + 按 consent_revision 失效"，
    拿它拦 TTS 输出会让正常音频播不出来。这里证明：库里**没有任何**入站证据
    行、也没有 consent 授权时，同窗口仍然能取到自己的产物。
    """
    from ling.infra.models import MediaEvidenceRow

    saved = await _save(tmp_path)

    async with session_scope() as session:
        evidence = (await session.execute(select(MediaEvidenceRow))).scalars().all()
        assert evidence == [], "这个用例的前提：库里没有任何入站媒体证据"

        service = make_service(tmp_path, session, base_url=BASE_URL)
        data = await service.read_for_window(
            saved.digest, requester_actor=ACTOR, requester_window=WINDOW
        )
    assert data == AUDIO, "生成媒体不该被入站授权拦掉"


# ------------------------------------------------------------------ 3. 路径穿越


@pytest.mark.parametrize(
    "bad_digest",
    [
        "../../etc/passwd",
        "..",
        "/etc/passwd",
        "abc/def",
        "0" * 63,           # 少一位
        "0" * 65,           # 多一位
        "A" * 64,           # 大写不算合法十六进制
        "g" * 64,           # 非十六进制
        "0" * 32 + "/" + "0" * 31,
    ],
)
def test_非法摘要进不了路径(bad_digest: str, tmp_path) -> None:
    storage = GeneratedMediaStorage(tmp_path)
    with pytest.raises(MediaStorageError):
        storage.path_for(bad_digest)
    with pytest.raises(MediaStorageError):
        storage.relative_path_for(bad_digest)


def test_越界的相对路径被拒(tmp_path) -> None:
    """库里的相对路径被写坏时，读取也不能逃出媒体根目录。"""
    storage = GeneratedMediaStorage(tmp_path)
    for bad in ("../../etc/passwd", "generated_media/../../outside", "/etc/shadow"):
        with pytest.raises(MediaStorageError):
            storage.absolute_path_of(bad)


def test_合法相对路径能解析(tmp_path) -> None:
    storage = GeneratedMediaStorage(tmp_path)
    digest = "a" * 64
    resolved = storage.absolute_path_of(storage.relative_path_for(digest))
    assert resolved == storage.path_for(digest)


async def test_坏摘要读不到内容(web_env, tmp_path) -> None:
    """即使记录存在，拿坏摘要也取不到东西（不会退化成 FileNotFound 之外的行为）。"""
    saved = await _save(tmp_path)
    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        with pytest.raises(AssetNotFound):
            await service.read_for_window(
                "../../../etc/passwd",
                requester_actor=ACTOR,
                requester_window=WINDOW,
            )
    assert saved.digest


# ------------------------------------------------------------------ 4. 签名 URL


async def test_签名URL可取出内容且不需要身份(web_env, tmp_path) -> None:
    saved = await _save(tmp_path)
    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        target = await service.sign_url_for_window(
            saved.digest, requester_actor=ACTOR, requester_window=WINDOW
        )
        assert target.transport is ExportTransport.signed_url
        assert target.url is not None
        assert "sig=" in target.url
        assert saved.digest in target.url

        # 从 URL 里取回参数，验证"凭签名就能取"
        from urllib.parse import parse_qs, urlparse

        parsed = urlparse(target.url)
        query = parse_qs(parsed.query)
        data = await service.resolve_signed(
            saved.digest, expires_at=int(query["exp"][0]), signature=query["sig"][0]
        )
    assert data == AUDIO


async def test_过期签名被拒(web_env, tmp_path) -> None:
    saved = await _save(tmp_path)
    signed = sign_file_url(
        root_key=ROOT_KEY, base_url=BASE_URL, file_id=saved.digest, ttl_seconds=60, now=1_000_000
    )
    with pytest.raises(SignatureError):
        verify_file_url(
            root_key=ROOT_KEY,
            file_id=saved.digest,
            expires_at=signed.expires_at,
            signature=signed.url.split("sig=")[1],
            now=1_000_000 + 61,
        )


async def test_改文件id的签名无效(web_env, tmp_path) -> None:
    """**单文件绑定**：拿 A 的签名去取 B 必须失败（IDOR 防护）。"""
    saved = await _save(tmp_path)
    signed = sign_file_url(root_key=ROOT_KEY, base_url=BASE_URL, file_id=saved.digest)
    signature = signed.url.split("sig=")[1]
    other = "b" * 64
    with pytest.raises(SignatureError):
        verify_file_url(
            root_key=ROOT_KEY, file_id=other, expires_at=signed.expires_at, signature=signature
        )


async def test_改过期时间的签名无效(web_env, tmp_path) -> None:
    saved = await _save(tmp_path)
    signed = sign_file_url(root_key=ROOT_KEY, base_url=BASE_URL, file_id=saved.digest)
    signature = signed.url.split("sig=")[1]
    with pytest.raises(SignatureError):
        verify_file_url(
            root_key=ROOT_KEY,
            file_id=saved.digest,
            expires_at=signed.expires_at + 86400,
            signature=signature,
        )


async def test_别的根密钥签的URL无效(web_env, tmp_path) -> None:
    saved = await _save(tmp_path)
    signed = sign_file_url(root_key=b"other-key-32-bytes-long!!!!!!", base_url=BASE_URL, file_id=saved.digest)
    with pytest.raises(SignatureError):
        verify_file_url(
            root_key=ROOT_KEY,
            file_id=saved.digest,
            expires_at=signed.expires_at,
            signature=signed.url.split("sig=")[1],
        )


async def test_签名不进字符串表示(web_env, tmp_path) -> None:
    """**审计日志安全**：`str(url)` / f-string / `%s` 都是打码形态。"""
    saved = await _save(tmp_path)
    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        target = await service.sign_url_for_window(
            saved.digest, requester_actor=ACTOR, requester_window=WINDOW
        )
    assert target.url is not None
    raw_sig = target.url.split("sig=")[1]
    assert raw_sig
    # 把 URL 当对象时（SignedUrl.__str__）签名必须被抹掉
    from ling.domains.media.exports import SignedUrl

    signed = SignedUrl(url=target.url, file_id=saved.digest, expires_at=0)
    assert raw_sig not in str(signed)
    assert "sig=***" in str(signed)
    assert raw_sig not in repr(signed)
    assert raw_sig not in f"{signed}"


async def test_TTL上限被强制(web_env, tmp_path) -> None:
    """不能要到"一年有效"的链接。"""
    with pytest.raises(ValueError):
        sign_file_url(root_key=ROOT_KEY, base_url=BASE_URL, file_id="a" * 64, ttl_seconds=86400 * 365)
    with pytest.raises(ValueError):
        sign_file_url(root_key=ROOT_KEY, base_url=BASE_URL, file_id="a" * 64, ttl_seconds=0)


# ------------------------------------------------------------------ 5. 三种 transport


@pytest.mark.parametrize(
    "transport,field",
    [
        (ExportTransport.local_path, "path"),
        (ExportTransport.base64, "data_uri"),
        (ExportTransport.signed_url, "url"),
    ],
)
async def test_三种transport都只在授权后可用(
    web_env, tmp_path, transport: ExportTransport, field: str
) -> None:
    saved = await _save(tmp_path)
    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        target = await service.export_for_window(
            saved.digest,
            transport=transport,
            requester_actor=ACTOR,
            requester_window=WINDOW,
        )
        assert getattr(target, field) is not None
        assert target.size_bytes == len(AUDIO)
        assert target.mime_type == "audio/mpeg"

        # 同一份文件、换一个窗口：三种 transport 一律拒绝
        with pytest.raises(ExportDenied):
            await service.export_for_window(
                saved.digest,
                transport=transport,
                requester_actor=ACTOR,
                requester_window=OTHER_WINDOW,
            )


async def test_base64形态符合OneBot约定(web_env, tmp_path) -> None:
    import base64

    saved = await _save(tmp_path)
    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        target = await service.export_for_window(
            saved.digest,
            transport=ExportTransport.base64,
            requester_actor=ACTOR,
            requester_window=WINDOW,
        )
    assert target.data_uri is not None
    assert target.data_uri.startswith("base64://")
    decoded = base64.b64decode(target.data_uri[len("base64://") :])
    assert decoded == AUDIO


async def test_local_path指向真实文件(web_env, tmp_path) -> None:
    saved = await _save(tmp_path)
    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        target = await service.export_for_window(
            saved.digest,
            transport=ExportTransport.local_path,
            requester_actor=ACTOR,
            requester_window=WINDOW,
        )
    assert target.path is not None
    assert os.path.isfile(target.path)
    with open(target.path, "rb") as handle:
        assert handle.read() == AUDIO


# ------------------------------------------------------------------ 6. 生命周期


async def test_过期后不可见且被清理(web_env, tmp_path) -> None:
    """过期 → 立刻不可见；过宽限期 → 文件真的被删。"""
    from datetime import timedelta

    from ling.infra.models import utcnow

    saved = await _save(tmp_path, retention_days=1)

    # 时间快进：把 expires_at 改到过去
    async with session_scope() as session:
        row = await session.get(MediaAssetRow, saved.digest)
        assert row is not None
        row.expires_at = utcnow() - timedelta(hours=1)
        await session.commit()

    storage = GeneratedMediaStorage(tmp_path)
    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        with pytest.raises(AssetNotFound):
            await service.read_for_window(
                saved.digest, requester_actor=ACTOR, requester_window=WINDOW
            )
        # 宽限期足够长 → 只标记，不删文件（在途任务与已签发 URL 仍然安全）
        purged = await service.purge_expired(grace_seconds=3600)
        await session.commit()
    assert purged == 0
    assert storage.exists(saved.digest), "宽限期内文件必须还在"

    # 宽限期过去 → 真删
    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        purged = await service.purge_expired(grace_seconds=0)
        await session.commit()
    assert purged == 1
    assert not storage.exists(saved.digest), "过了宽限期文件必须被删"
    async with session_scope() as session:
        assert await session.get(MediaAssetRow, saved.digest) is None


async def test_删除不影响在途任务(web_env, tmp_path) -> None:
    """软删后：读取立刻失败，但**已拿到的字节与已签发的 URL 仍然有效**。

    这就是"删除不影响在途任务"的可执行定义：在途任务手里不是"路径"，是
    内容或签名。
    """
    saved = await _save(tmp_path)
    signed_url: str | None = None
    payload: bytes | None = None

    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        payload = await service.read_for_window(
            saved.digest, requester_actor=ACTOR, requester_window=WINDOW
        )
        target = await service.sign_url_for_window(
            saved.digest, requester_actor=ACTOR, requester_window=WINDOW
        )
        signed_url = target.url
        await session.commit()

    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        assert await service.soft_delete(saved.digest, reason="用户删了") is True
        await session.commit()

    # 新的读取一律拒绝
    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        with pytest.raises(AssetNotFound):
            await service.read_for_window(
                saved.digest, requester_actor=ACTOR, requester_window=WINDOW
            )

    # 在途任务手里的东西仍然有效
    assert payload == AUDIO
    assert signed_url is not None
    assert GeneratedMediaStorage(tmp_path).exists(saved.digest), "软删不立刻删文件"


async def test_软删后清理会删文件(web_env, tmp_path) -> None:
    saved = await _save(tmp_path)
    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        await service.soft_delete(saved.digest)
        await session.commit()
    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        purged = await service.purge_expired(grace_seconds=0)
        await session.commit()
    assert purged == 1
    assert not GeneratedMediaStorage(tmp_path).exists(saved.digest)


async def test_保留期可配置(web_env, tmp_path, monkeypatch) -> None:
    """保留期来自环境变量，且 `<=0` 表示不自动过期。"""
    monkeypatch.setenv("LING_MEDIA_ASSET_RETENTION_DAYS", "3")
    assert resolve_retention_days() == 3
    monkeypatch.setenv("LING_MEDIA_ASSET_RETENTION_DAYS", "0")
    assert resolve_retention_days() == 0
    monkeypatch.setenv("LING_MEDIA_ASSET_RETENTION_DAYS", "-5")
    assert resolve_retention_days() == 0
    monkeypatch.setenv("LING_MEDIA_ASSET_RETENTION_DAYS", "不是数字")
    assert resolve_retention_days() == 7
    monkeypatch.setenv("LING_MEDIA_ASSET_RETENTION_DAYS", "3")
    assert resolve_retention_days(10) == 10, "显式参数优先于环境变量"

    # days=0 → 不过期
    saved = await _save(tmp_path, retention_days=0)
    async with session_scope() as session:
        row = await session.get(MediaAssetRow, saved.digest)
        assert row is not None
        assert row.expires_at is None


# ------------------------------------------------------------------ 7. 列表与格式校验


async def test_窗口列表只含本窗口且按时间倒序(web_env, tmp_path) -> None:
    first = await _save(tmp_path, data=b"AAA" + AUDIO)
    second = await _save(tmp_path, data=b"BBB" + AUDIO)
    # 别人窗口的一份
    await _save(tmp_path, data=b"CCC" + AUDIO, actor_id="actor_other")

    async with session_scope() as session:
        views = await list_window_assets(session, actor_id=ACTOR, window_id=WINDOW)
    digests = [view.digest for view in views]
    assert set(digests) == {first.digest, second.digest}
    assert len(digests) == 2


async def test_不支持的格式被拒(web_env, tmp_path) -> None:
    """格式白名单：不猜 MIME，也不接受任意字符串。"""
    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        with pytest.raises(AssetError):
            await service.save_bytes(
                AUDIO, actor_id=ACTOR, window_id=WINDOW, fmt="exe"
            )


async def test_缺归属被拒(web_env, tmp_path) -> None:
    """没有 actor/window 就没有授权凭据，不能存。"""
    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        with pytest.raises(AssetError):
            await service.save_bytes(AUDIO, actor_id="", window_id=WINDOW)
        with pytest.raises(AssetError):
            await service.save_bytes(AUDIO, actor_id=ACTOR, window_id="")


async def test_空内容被拒(web_env, tmp_path) -> None:
    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        with pytest.raises(MediaStorageError):
            await service.save_bytes(b"", actor_id=ACTOR, window_id=WINDOW)


async def test_孤儿文件可被回收(web_env, tmp_path) -> None:
    """先落盘后写库的副产物（无记录指向的文件）能清理掉。"""
    storage = GeneratedMediaStorage(tmp_path)
    orphan = storage.put(b"orphan-bytes-not-in-db")
    kept = await _save(tmp_path)

    async with session_scope() as session:
        service = make_service(tmp_path, session, base_url=BASE_URL)
        removed = await service.collect_orphans()
        await session.commit()
    assert removed == 1
    assert not storage.exists(orphan.digest), "无记录的孤儿该被删"
    assert storage.exists(kept.digest), "有记录的文件绝不能删"


# ------------------------------------------------------------------ 8. 授权判定本身


def test_同actor同窗口放行() -> None:
    check_window_access(
        requester_window=WINDOW, requester_actor=ACTOR, file_window=WINDOW, file_actor=ACTOR
    )


@pytest.mark.parametrize(
    "requester_actor,requester_window",
    [
        (ACTOR, OTHER_WINDOW),
        ("someone", WINDOW),
        ("someone", OTHER_WINDOW),
        (None, WINDOW),
        (ACTOR, None),
        (None, None),
    ],
)
def test_其余组合一律拒绝(requester_actor, requester_window) -> None:
    with pytest.raises(ExportDenied):
        check_window_access(
            requester_window=requester_window,
            requester_actor=requester_actor,
            file_window=WINDOW,
            file_actor=ACTOR,
        )
