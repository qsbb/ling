"""Web 端图片上传：让她能看见你发的东西。

## 为什么需要这个

QQ 那边有媒体通道，**Web 这边完全没有**——用户在网页上根本没法给她看任何东西。
而"她能看见你发的东西"是拟人的一部分。

## 三个设计点

1. **必须有 `media.inbound` 授权**。这是隐私门，不能为了让功能"好用"而绕过；
   但缺授权时必须给可执行的下一步，而不是一个 500。
2. **文件名由 evidence id 生成**，不接受客户端文件名——那是一条路径穿越入口。
3. **只收图片**。收别的类型等于给自己开一个任意文件落盘的洞。

## 描述是异步的

上传只落盘 + 登记证据，**不在这里生成描述**。描述由 worker 的
`media.describe` 做，且拿不到视觉模型就留空、下次再试——不写占位符。
"""

from __future__ import annotations

import struct
import zlib
from pathlib import Path

import pytest

from ling.infra.db import session_scope
from ling.infra.models import MediaEvidenceRow
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


def _tiny_png() -> bytes:
    """64x64 左红右蓝，140 字节。不依赖 PIL。"""

    def chunk(tag: bytes, data: bytes) -> bytes:
        body = tag + data
        return (
            struct.pack(">I", len(data))
            + body
            + struct.pack(">I", zlib.crc32(body) & 0xFFFFFFFF)
        )

    width = height = 64
    raw = b""
    for _y in range(height):
        row = b""
        for x in range(width):
            row += bytes([220, 30, 30]) if x < width // 2 else bytes([30, 60, 220])
        raw += b"\x00" + row
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw, 9))
        + chunk(b"IEND", b"")
    )


async def _grant(logged_in: TestEnv) -> None:
    r = await logged_in.client.put(
        "/v1/consents", json={"scope": "media.inbound", "granted": True}
    )
    assert r.status_code == 200, r.text


async def _session(logged_in: TestEnv, title: str = "图片") -> str:
    r = await logged_in.client.post("/v1/sessions", json={"title": title})
    return r.json()["session_id"]


async def _upload(logged_in: TestEnv, session_id: str, data: bytes, mime: str):
    return await logged_in.client.post(
        f"/v1/sessions/{session_id}/media",
        files={"upload": ("x.png", data, mime)},
    )


# ---------------------------------------------------------------- 授权门


async def test_upload_requires_media_consent(logged_in: TestEnv) -> None:
    """没有 media.inbound 授权时必须拒绝，且给出可执行的下一步。"""
    sid = await _session(logged_in, "未授权")
    r = await _upload(logged_in, sid, _tiny_png(), "image/png")
    assert r.status_code == 403, r.text
    body = r.json()
    assert body["error"]["code"] == "forbidden"
    assert "media.inbound" in body["error"]["message"], body["error"]["message"]


async def test_upload_works_after_consent(logged_in: TestEnv) -> None:
    await _grant(logged_in)
    sid = await _session(logged_in, "已授权")
    png = _tiny_png()
    r = await _upload(logged_in, sid, png, "image/png")
    assert r.status_code == 200, r.text
    body = r.json()
    assert body["bytes"] == len(png)
    assert body["mime"] == "image/png"
    assert body["evidence_id"].startswith("media_")


# ---------------------------------------------------------------- 落盘


async def test_file_is_written_with_evidence_id_as_name(logged_in: TestEnv) -> None:
    """文件名由 evidence id 生成，**不用客户端文件名**。

    用客户端文件名是一条路径穿越入口（`../../etc/passwd`）。
    """
    await _grant(logged_in)
    sid = await _session(logged_in, "落盘")
    r = await _upload(logged_in, sid, _tiny_png(), "image/png")
    eid = r.json()["evidence_id"]

    async with session_scope() as session:
        row = await session.get(MediaEvidenceRow, eid)
    assert row is not None
    assert row.media_ref.startswith("file:"), row.media_ref
    path = Path(row.media_ref[5:])
    assert path.name == f"{eid}.png", f"文件名没按 evidence id 生成：{path.name}"
    assert path.read_bytes() == _tiny_png()


async def test_evidence_is_bound_to_the_session(logged_in: TestEnv) -> None:
    """证据必须挂在**这个窗口**上，否则上下文 lane 找不到它。

    媒体 lane 是按 `described_in_window(window_id)` 取的。
    """
    await _grant(logged_in)
    sid = await _session(logged_in, "绑定")
    eid = (await _upload(logged_in, sid, _tiny_png(), "image/png")).json()["evidence_id"]
    async with session_scope() as session:
        row = await session.get(MediaEvidenceRow, eid)
    assert row is not None
    assert row.window_id == sid


# ---------------------------------------------------------------- 类型门


@pytest.mark.parametrize("mime", ["text/plain", "application/pdf", "image/svg+xml"])
async def test_non_image_is_rejected(logged_in: TestEnv, mime: str) -> None:
    """只收图片。收别的类型等于开一个任意文件落盘的洞。"""
    await _grant(logged_in)
    sid = await _session(logged_in, "类型")
    r = await _upload(logged_in, sid, b"hello", mime)
    assert r.status_code == 422, r.text
    assert "只支持图片" in r.json()["error"]["message"]


async def test_empty_upload_is_rejected(logged_in: TestEnv) -> None:
    await _grant(logged_in)
    sid = await _session(logged_in, "空")
    r = await _upload(logged_in, sid, b"", "image/png")
    assert r.status_code == 422, r.text


# ---------------------------------------------------------------- 描述


async def test_uploaded_evidence_starts_undescribed(logged_in: TestEnv) -> None:
    """上传只落盘 + 登记，**不在这里生成描述**。

    描述要视觉模型；拿不到就留空、下次再试，不写占位符。
    """
    await _grant(logged_in)
    sid = await _session(logged_in, "未描述")
    eid = (await _upload(logged_in, sid, _tiny_png(), "image/png")).json()["evidence_id"]
    async with session_scope() as session:
        row = await session.get(MediaEvidenceRow, eid)
    assert row is not None
    assert row.description is None, f"上传接口不该顺手写描述：{row.description!r}"


async def test_local_fetcher_reads_file_ref(logged_in: TestEnv) -> None:
    """`file:` 引用必须能不经任何渠道直接读——**但只能读媒体目录里的文件**。

    Web 上传的图没有 OneBot 也要能描述。原来 worker 直接用
    `connector.fetch_media`，没有渠道时整段跳过，图片永远得不到描述。

    ## 为什么这个用例改成写进 `data_dir/media`

    旧版本把探针文件写到 `/tmp/ling_fetch_probe.png` 再读。那正是本次要
    堵的洞：`file:` 分支原来无前缀约束、无大小上限，`file:/etc/passwd`
    能读任意文件、`file:/dev/zero` 能把进程读死。现在要求解析后必须落在
    媒体目录内，所以探针文件也必须落在那里——这不是"为了跑通放宽断言"，
    而是旧用例恰好演示了越界读取这条路（`/tmp` 在媒体目录之外）。
    """
    from ling.worker import make_media_fetcher

    media_root = logged_in.settings.data_dir / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    path = media_root / "ling_fetch_probe.png"
    path.write_bytes(_tiny_png())
    try:
        # 媒体目录**显式传入**：不传会回落到进程级 get_settings().data_dir，
        # 与测试用的临时 data_dir 不是同一个（会误报越界）。
        fetch = make_media_fetcher(None, media_root=media_root)  # 没有渠道
        data, mime = await fetch(f"file:{path}")
        assert data == _tiny_png()
        assert mime == "image/png", mime
    finally:
        path.unlink(missing_ok=True)


async def test_remote_ref_without_channel_fails_loudly(logged_in: TestEnv) -> None:  # noqa: ARG001
    """没有渠道时远程引用要报错，不能静默返回空。

    静默返回空会让上层把"没拿到"当成"内容为空"，判定成不可重试的失败。
    """
    import pytest as _pytest

    from ling.worker import make_media_fetcher

    fetch = make_media_fetcher(None)
    with _pytest.raises(RuntimeError):
        await fetch("https://example.invalid/a.png")


# ---------------------------------------------------------------- file: 的边界


async def test_file_ref_outside_media_dir_is_rejected(logged_in: TestEnv) -> None:
    """媒体目录之外的路径必须**明确报错**，不能静默返回空。

    这是本次修复的核心：原来 `file:` 分支是
    `Path(media_ref[5:]).read_bytes()`——无前缀约束、无大小上限，
    于是 `file:/etc/passwd` 能读任意文件、`file:/dev/zero` 能把进程读死。

    判定用 `resolve()` + `is_relative_to()`，**不是字符串前缀**：
    `data/media-evil` 以 `data/media` 开头，字符串前缀会误放行。
    """
    import pytest as _pytest

    from ling.worker import MediaRefError, make_media_fetcher

    media_root = logged_in.settings.data_dir / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    fetch = make_media_fetcher(None, media_root=media_root)

    # 1) 系统文件
    with _pytest.raises(MediaRefError):
        await fetch("file:/etc/passwd")
    # 2) file:// URI 形式（带 authority）直接拒绝，不去猜语义
    with _pytest.raises(MediaRefError):
        await fetch("file:///etc/passwd")
    # 3) **兄弟目录**：媒体目录是 <data_dir>/media，这个是 <data_dir>/media-evil。
    #    字符串前缀 `.../media` 会命中它，所以这条专门用来钉住 resolve+is_relative_to。
    data_dir = logged_in.settings.data_dir
    sibling = data_dir / "media-evil"
    sibling.mkdir(parents=True, exist_ok=True)
    evil_file = sibling / "evil.png"
    evil_file.write_bytes(_tiny_png())
    try:
        with _pytest.raises(MediaRefError):
            await fetch(f"file:{evil_file}")
    finally:
        evil_file.unlink(missing_ok=True)
        sibling.rmdir()
    # 4) `..` 穿越：从媒体目录内部指出去
    with _pytest.raises(MediaRefError):
        await fetch(f"file:{media_root / '..' / 'media-evil' / 'evil.png'}")
    # 5) 相对路径：按 cwd 解析的位置不可预期，直接拒绝
    with _pytest.raises(MediaRefError):
        await fetch("file:relative.png")


async def test_file_ref_oversize_is_rejected(logged_in: TestEnv) -> None:
    """超过上限必须报错（默认上限 = LING_MAX_BODY_BYTES）。"""
    import pytest as _pytest

    from ling.worker import MediaRefError, make_media_fetcher

    media_root = logged_in.settings.data_dir / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    big = media_root / "too_big.png"
    big.write_bytes(b"z" * 4096)
    try:
        # 显式给一个很小的上限，避免依赖部署里的 32MiB 默认值
        fetch = make_media_fetcher(None, media_root=media_root, max_bytes=1024)
        with _pytest.raises(MediaRefError) as exc:
            await fetch(f"file:{big}")
        assert "超过上限" in str(exc.value)
    finally:
        big.unlink(missing_ok=True)


async def test_file_ref_symlink_escape_is_rejected(logged_in: TestEnv) -> None:
    """媒体目录里的符号链接指向外部时也要被拒（resolve() 会展开它）。"""
    import os

    import pytest as _pytest

    from ling.worker import MediaRefError, make_media_fetcher

    data_dir = logged_in.settings.data_dir
    media_root = data_dir / "media"
    media_root.mkdir(parents=True, exist_ok=True)
    outside = data_dir / "outside_secret.txt"
    outside.write_text("secret", encoding="utf-8")
    link = media_root / "link.png"
    try:
        os.symlink(outside, link)
    except OSError:
        import pytest

        pytest.skip("当前环境不支持创建符号链接")
    try:
        fetch = make_media_fetcher(None, media_root=media_root)
        with _pytest.raises(MediaRefError):
            await fetch(f"file:{link}")
    finally:
        link.unlink(missing_ok=True)
        outside.unlink(missing_ok=True)
