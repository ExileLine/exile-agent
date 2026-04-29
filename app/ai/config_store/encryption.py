import base64
import hashlib
import json
from typing import Any

from cryptography.fernet import Fernet

from app.core.config import get_config


# 这是首期本地加密实现，目标是避免 secret 明文落库或通过接口回显。
# 生产环境后续可以把这里替换成 KMS/Vault，调用方不需要改变。
def encrypt_secret(value: str | None) -> str | None:
    """Encrypt a secret for database storage."""

    if value is None:
        return None
    if value == "":
        return ""
    return _fernet().encrypt(value.encode("utf-8")).decode("utf-8")


def decrypt_secret(value: str | None) -> str | None:
    """Decrypt a secret loaded from database storage."""

    if value is None:
        return None
    if value == "":
        return ""
    return _fernet().decrypt(value.encode("utf-8")).decode("utf-8")


def encrypt_secret_mapping(value: dict[str, Any] | None) -> dict[str, str]:
    """Encrypt each value in a mapping while preserving keys for display/routing.

    MCP headers/env 的 key 本身通常需要在管理页展示或用于调试，
    value 才是敏感内容，因此这里保留 key、加密 value。
    """

    encrypted: dict[str, str] = {}
    for key, raw_value in (value or {}).items():
        encrypted[str(key)] = encrypt_secret(_serialize_secret_value(raw_value)) or ""
    return encrypted


def decrypt_secret_mapping(value: dict[str, str] | None) -> dict[str, str]:
    """对存储的秘密映射中的每个值进行解密。"""

    decrypted: dict[str, str] = {}
    for key, encrypted_value in (value or {}).items():
        decrypted[str(key)] = decrypt_secret(encrypted_value) or ""
    return decrypted


def _serialize_secret_value(value: Any) -> str:
    if isinstance(value, str):
        return value
    return json.dumps(value, ensure_ascii=False, sort_keys=True)


def _fernet() -> Fernet:
    # Fernet 要求 32-byte urlsafe base64 key。
    # 这里从项目 SECRET_KEY 派生固定密钥，保证重启后仍能解密历史配置。
    secret_key = get_config().SECRET_KEY
    digest = hashlib.sha256(secret_key.encode("utf-8")).digest()
    return Fernet(base64.urlsafe_b64encode(digest))
