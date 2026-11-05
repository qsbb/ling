"""密钥诊断：区分"没存过"与"存过但解不开"。

两者都让 `read_secret` 返回 None，但原因完全不同：
- `missing`：从来没存过 → 去设置里填密钥
- `undecryptable`：存过但解不开（根密钥变了，通常是数据目录被换过）
  → 密钥还在，问题在 root.key

不区分的话，第二种会显示成"还没有保存密钥"——用户明明存过，
会被这句话带偏，反复重填也修不好。
"""

from __future__ import annotations

import base64

import pytest

from ling.infra.db import session_scope
from ling.infra.models import ProviderCredential
from ling.settings.service import ConfigService
from tests.integration.conftest import TestEnv

pytestmark = pytest.mark.w0_web


class _Box:
    """可控的加解密容器：用来制造"解不开"的情形。"""

    def __init__(self, *, fail: bool = False) -> None:
        self.fail = fail

    def encrypt(self, plaintext: str) -> str:
        return base64.b64encode(b"x" * 12 + plaintext.encode()).decode()

    def decrypt(self, ciphertext: str) -> str:
        if self.fail:
            raise ValueError("根密钥不匹配")
        return "plain"


async def _seed_credential(provider_id: str, box: _Box) -> None:
    async with session_scope() as session:
        session.add(
            ProviderCredential(
                id=f"cred_{provider_id}",
                provider_id=provider_id,
                ciphertext=box.encrypt("secret"),
                last4="abcd",
                fingerprint="fp",
            )
        )


async def test_missing_when_no_record(logged_in: TestEnv) -> None:
    async with session_scope() as session:
        svc = ConfigService(session, _Box(), test_mode_default=True, persona_id="p")
        assert await svc.secret_diagnosis("never_saved") == "missing"


async def test_ok_when_decryptable(logged_in: TestEnv) -> None:
    await _seed_credential("good_provider", _Box())
    async with session_scope() as session:
        svc = ConfigService(session, _Box(), test_mode_default=True, persona_id="p")
        assert await svc.secret_diagnosis("good_provider") == "ok"


async def test_undecryptable_when_key_changed(logged_in: TestEnv) -> None:
    """存过但根密钥变了——必须与"没存过"区分开。"""
    await _seed_credential("broken_provider", _Box())
    async with session_scope() as session:
        svc = ConfigService(
            session, _Box(fail=True), test_mode_default=True, persona_id="p"
        )
        assert await svc.secret_diagnosis("broken_provider") == "undecryptable"


async def test_error_message_differs(logged_in: TestEnv) -> None:
    """两种原因给出的提示文案必须不同，且都指向可执行的下一步。"""
    from ling.models.openai_compatible import OpenAICompatibleProvider
    from ling.ports.model import ModelError, ModelErrorKind, ModelRequest

    async def _message(hint: str) -> str:
        provider = OpenAICompatibleProvider(
            provider_id="p1",
            base_url="http://127.0.0.1:1/v1",
            api_key=None,
            secret_hint=lambda _pid: hint,
        )
        with pytest.raises(ModelError) as exc:
            async for _ in provider.stream(ModelRequest(model_id="m", messages=[])):
                pass
        assert exc.value.kind is ModelErrorKind.missing_secret
        return str(exc.value)

    missing = await _message("missing")
    broken = await _message("undecryptable")
    assert "还没有保存密钥" in missing
    assert "无法解密" in broken
    assert "root.key" in broken, "解不开的情况没有给出排查方向"
    assert missing != broken


async def test_provider_without_hint_falls_back(logged_in: TestEnv) -> None:
    """没有 hint 回调时退回通用文案（不能让异常路径本身崩）。"""
    from ling.models.openai_compatible import OpenAICompatibleProvider
    from ling.ports.model import ModelError, ModelRequest

    provider = OpenAICompatibleProvider(
        provider_id="p2", base_url="http://127.0.0.1:1/v1", api_key=None
    )
    with pytest.raises(ModelError) as exc:
        async for _ in provider.stream(ModelRequest(model_id="m", messages=[])):
            pass
    assert "还没有保存密钥" in str(exc.value)
