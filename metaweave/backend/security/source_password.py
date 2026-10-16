"""仅加解密登记的源库密码。"""

import os

from cryptography.fernet import Fernet


def _fernet() -> Fernet:
    key = os.getenv("SOURCE_CREDENTIAL_KEY")
    if not key:
        raise ValueError("缺少 SOURCE_CREDENTIAL_KEY")
    try:
        return Fernet(key.encode("ascii"))
    except Exception as exc:
        raise ValueError("SOURCE_CREDENTIAL_KEY 格式无效") from exc


def encrypt_password(plaintext: str) -> str:
    if not isinstance(plaintext, str):
        raise ValueError("源库密码必须是字符串")
    return _fernet().encrypt(plaintext.encode("utf-8")).decode("ascii")


def decrypt_password(ciphertext: str) -> str:
    try:
        return _fernet().decrypt(ciphertext.encode("ascii")).decode("utf-8")
    except Exception as exc:
        raise ValueError("源库密码无法解密；请检查密钥和登记记录") from exc
