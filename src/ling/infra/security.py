"""安全原语：口令哈希、会话令牌、供应商密钥加密与遮蔽。

- 口令：scrypt（标准库），每用户独立 salt。
- 会话令牌：只在浏览器 cookie 中出现；库中只存 SHA-256 哈希。
- 供应商密钥：AES-256-GCM 加密后入库；根密钥来自环境或数据目录本地文件。
  对外 API 只返回后四位与指纹，原文永不回传。
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
import secrets
from pathlib import Path

from cryptography.hazmat.primitives.ciphers.aead import AESGCM

from ling.config import Settings

_SCRYPT_N = 2**14
_SCRYPT_R = 8
_SCRYPT_P = 1
_DKLEN = 32


# ------------------------------------------------------------------ 口令

DUMMY_PASSWORD_HASH = (
    "scrypt$16384$8$1$bGluZy1kdW1teS1zYWx0LTE="
    "$PuKj0gGiboD8oyCD0qh2MrNtXgGBrp7YuhOFak4Wc60="
)
"""恒定耗时的固定哈希：**用户不存在 / 账号被禁用**时也要跑一次 scrypt。

为什么需要：``verify_password`` 是 scrypt（实测单次约 16ms @ N=2^14）。
如果"用户不存在"时跳过它，响应会快一个数量级——错误体写得再一致，
**时间本身就在回答"这个账号存在吗"**，可用来枚举账号。

所以那条路径必须付同样的 CPU 代价。这里给一个永远不可能匹配的哈希：
明文是一段随机字符串且没有保存在任何地方，参数与 ``hash_password``
完全一致（N=2^14, r=8, p=1, dklen=32），因此耗时相同。

固定 salt 不影响用途：它从不与真实口令比较，只用于"花掉同样的时间"。
"""

def hash_password(password: str) -> str:
    salt = os.urandom(16)
    digest = hashlib.scrypt(
        password.encode("utf-8"), salt=salt, n=_SCRYPT_N, r=_SCRYPT_R, p=_SCRYPT_P, dklen=_DKLEN
    )
    salt_b64 = base64.b64encode(salt).decode()
    digest_b64 = base64.b64encode(digest).decode()
    return f"scrypt${_SCRYPT_N}${_SCRYPT_R}${_SCRYPT_P}${salt_b64}${digest_b64}"


def verify_password(password: str, stored: str) -> bool:
    try:
        scheme, n, r, p, salt_b64, digest_b64 = stored.split("$")
        if scheme != "scrypt":
            return False
        salt = base64.b64decode(salt_b64)
        expected = base64.b64decode(digest_b64)
        actual = hashlib.scrypt(
            password.encode("utf-8"),
            salt=salt,
            n=int(n),
            r=int(r),
            p=int(p),
            dklen=len(expected),
        )
        return hmac.compare_digest(actual, expected)
    except (ValueError, TypeError):
        return False


# ------------------------------------------------------------------ 令牌


def new_token() -> str:
    return secrets.token_urlsafe(32)


def hash_token(token: str) -> str:
    return hashlib.sha256(token.encode("utf-8")).hexdigest()


def new_id(prefix: str) -> str:
    return f"{prefix}_{secrets.token_hex(12)}"


# ------------------------------------------------------------------ 密钥加密


def _load_root_key(settings: Settings) -> bytes:
    """根密钥优先级：环境变量 → 数据目录文件（首次自动生成）。"""
    if settings.root_key:
        raw = settings.root_key.encode("utf-8")
        return hashlib.sha256(raw).digest()
    path: Path = settings.secret_key_path
    if path.exists():
        return base64.b64decode(path.read_text(encoding="utf-8").strip())
    key = os.urandom(32)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(base64.b64encode(key).decode(), encoding="utf-8")
    path.chmod(0o600)
    return key


class SecretBox:
    """对称加密容器。同一进程内复用根密钥。"""

    def __init__(self, settings: Settings) -> None:
        self._key = _load_root_key(settings)
        self._aead = AESGCM(self._key)

    def encrypt(self, plaintext: str) -> str:
        nonce = os.urandom(12)
        ct = self._aead.encrypt(nonce, plaintext.encode("utf-8"), b"ling-secret-v1")
        return base64.b64encode(nonce + ct).decode()

    def decrypt(self, ciphertext: str) -> str:
        raw = base64.b64decode(ciphertext)
        nonce, ct = raw[:12], raw[12:]
        return self._aead.decrypt(nonce, ct, b"ling-secret-v1").decode("utf-8")

    @staticmethod
    def last4(plaintext: str) -> str:
        return plaintext[-4:] if len(plaintext) >= 4 else ""

    @staticmethod
    def fingerprint(plaintext: str) -> str:
        return hashlib.sha256(plaintext.encode("utf-8")).hexdigest()[:16]


def mask_secret(plaintext: str | None) -> str:
    """只用于展示：****abcd。"""
    if not plaintext:
        return ""
    tail = plaintext[-4:] if len(plaintext) >= 4 else ""
    return f"****{tail}"
