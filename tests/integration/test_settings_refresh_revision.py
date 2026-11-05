"""跨进程配置刷新标记：只认**已发布**修订，且换密钥必须让标记动起来。

## 这两条各自防的是什么

**草稿抬标记**：``current_revision()`` 曾按最大修订号取（含草稿）。只要有人
在设置页存过一次草稿而没发布，标记就被抬到一个"发布也不会再变"的数字上
（``publish()`` 只改行 state，不新增号）。进程刷新一轮后把
``loaded_revision`` 记成草稿号，之后 ``stale()`` 永远是 False——**所有副本
与 worker 从此不再重载**。这是最严重的一条：一次未发布的操作就能让刷新
机制永久失效，而且完全静默。

**换密钥不动标记**：``set_secret()`` 只 upsert 凭据表 + 同步 ``has_secret``
标记，不产生新修订。跨进程传播的唯一信号就是修订号，所以其他副本与 worker
永远认为"配置没变"，继续用旧密钥：一部分请求失败、worker 的记忆提炼静默
退回正则。用户感知是"它有时能聊有时不能，还记不住话"。
"""

from __future__ import annotations

import base64

import pytest

from ling.infra.db import session_scope
from ling.settings.refresh import RevisionWatcher, current_revision, refresh_secrets
from ling.settings.service import ConfigService
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web

PROVIDER = "p1"


class _Box:
    """可控的加解密容器：测试只关心"能不能解密出密钥"，不关心算法。"""

    def encrypt(self, plaintext: str) -> str:
        return base64.b64encode(b"x" * 12 + plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        return base64.b64decode(ciphertext)[12:].decode()


def _svc(session) -> ConfigService:
    return ConfigService(session, _Box(), test_mode_default=True, persona_id="persona_ling")


async def _publish_baseline() -> int:
    """发布一份带 provider 的基线配置，返回其修订号。"""
    async with session_scope() as session:
        svc = _svc(session)
        draft = await svc.save_draft(
            providers=[
                {
                    "provider_id": PROVIDER,
                    "kind": "openai_compatible",
                    "base_url": "https://example.test/v1",
                }
            ],
            note="baseline",
        )
        return await svc.publish(draft.revision)


async def test_draft_does_not_mark_stale(web_env: TestEnv) -> None:
    """存草稿（未发布）不能让任何进程误判为"配置变了"。

    变异验证：把 ``current_revision`` 的 ``state == 'published'`` 过滤去掉，
    这条会 FAIL——草稿号盖过已发布号，标记被抬高。
    """
    published = await _publish_baseline()

    async with session_scope() as session:
        watcher = RevisionWatcher()
        watcher.mark_loaded(await current_revision(session))

    async with session_scope() as session:
        svc = _svc(session)
        draft = await svc.save_draft(note="存了草稿但没发布")
        assert draft.state == "draft"
        assert draft.revision > published, "草稿必须占一个更大的修订号（前提没变）"

    async with session_scope() as session:
        assert await current_revision(session) == published, (
            "草稿抬高了刷新标记：发布前所有进程都不该重载"
        )
        assert await watcher.stale(session) is False, "草稿让进程误判为 stale"


async def test_publish_marks_stale(web_env: TestEnv) -> None:
    """发布草稿后，其他进程必须能感知到（``publish`` 不新增号，靠 state 生效）。

    变异验证：回退 ``current_revision`` 的过滤后，这条会 FAIL——标记停在
    草稿号上，发布不再改变它，于是永远 False。
    """
    await _publish_baseline()

    async with session_scope() as session:
        watcher = RevisionWatcher()
        watcher.mark_loaded(await current_revision(session))

    async with session_scope() as session:
        svc = _svc(session)
        draft = await svc.save_draft(note="待发布")
        assert await watcher.stale(session) is False, "存草稿不该让进程重载"
        await svc.publish(draft.revision)

    async with session_scope() as session:
        assert await current_revision(session) == draft.revision
        assert await watcher.stale(session) is True, "发布后进程必须重载"


async def test_secret_change_is_visible_to_worker(web_env: TestEnv) -> None:
    """换密钥必须让 worker 侧的修订号比对失败，从而重建路由、拿到新密钥。

    worker 不用 ``RevisionWatcher``，只看 ``current_revision()``（见
    ``worker.refresh_memory_router_if_stale``）；这里复刻那条比对。

    变异验证：去掉 ``set_secret`` 里的 ``_bump_for_secret_change``，这条会 FAIL。
    """
    published = await _publish_baseline()

    async with session_scope() as session:
        before = await current_revision(session)
        assert before == published

    async with session_scope() as session:
        await _svc(session).set_secret(PROVIDER, "sk-new-key-0001")

    async with session_scope() as session:
        after = await current_revision(session)
    assert after != before, "换密钥后修订号没变：worker 会继续用旧密钥静默降级"

    # 新密钥确实能被刷新路径读进进程缓存（不是只改了标记）
    cache: dict[str, str] = {}
    async with session_scope() as session:
        written = await refresh_secrets(
            session, cache=cache, box=_Box(), persona_id="persona_ling",
            test_mode_default=True,
        )
    assert written >= 1
    assert cache[PROVIDER] == "sk-new-key-0001"

    # 再换一次也要感知（第二次起走的是"更新已有凭据"分支）
    async with session_scope() as session:
        await _svc(session).set_secret(PROVIDER, "sk-new-key-0002")
    async with session_scope() as session:
        assert await current_revision(session) != after
    assert cache[PROVIDER] != "sk-new-key-0002", "缓存是旧的，需要走一次刷新"


async def test_delete_secret_marks_stale(web_env: TestEnv) -> None:
    """删密钥同样要能传播：否则其他副本会继续用已作废的密钥。"""
    await _publish_baseline()
    async with session_scope() as session:
        await _svc(session).set_secret(PROVIDER, "sk-to-delete-0001")
    async with session_scope() as session:
        before = await current_revision(session)

    async with session_scope() as session:
        await _svc(session).delete_secret(PROVIDER)
    async with session_scope() as session:
        assert await current_revision(session) != before


async def test_refresh_marker_converges(web_env: TestEnv) -> None:
    """迁移安全：刷新一次后标记必须收敛，不能无限重载。

    进程记录的是"自己用当前语义算出的号"，不是旧实现里含草稿的最大号——
    升级必然伴随重启，重启后由 ``app.py`` 用新语义写入。这里验证收敛性：
    ``mark_loaded`` 之后再比对，必须立刻相等。
    """
    await _publish_baseline()
    async with session_scope() as session:
        svc = _svc(session)
        await svc.save_draft(note="库里留一份草稿，模拟升级前的残留")
    async with session_scope() as session:
        svc = _svc(session)
        await svc.set_secret(PROVIDER, "sk-converge-0001")

    async with session_scope() as session:
        watcher = RevisionWatcher()
        # 启动路径（app.py）就是这样写的：先算，再记
        watcher.mark_loaded(await current_revision(session))
        assert await watcher.stale(session) is False, "刚加载完就 stale → 会无限重载"
        assert await watcher.stale(session) is False, "第二次比对仍必须相等"


async def test_rollback_to_older_revision_is_detected(web_env: TestEnv) -> None:
    """回滚把**更旧**的号重新发布，标记会变小——比较必须用 ``!=`` 而不是 ``>``。

    用 ``>`` 的话，回滚对已经跑到新号的进程完全不可见：它们会继续用被
    回滚掉的那版配置，而且同样静默。
    """
    first = await _publish_baseline()
    async with session_scope() as session:
        svc = _svc(session)
        second = await svc.save_draft(note="第二版")
        await svc.publish(second.revision)

    async with session_scope() as session:
        watcher = RevisionWatcher()
        watcher.mark_loaded(await current_revision(session))
        assert await watcher.stale(session) is False

    async with session_scope() as session:
        await _svc(session).rollback(first)
    async with session_scope() as session:
        assert await current_revision(session) == first
        assert await watcher.stale(session) is True, "回滚到旧号也必须被感知"

