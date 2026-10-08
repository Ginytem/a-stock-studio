# -*- coding: utf-8 -*-
"""AI 固定接口 TOTP 认证（RFC 6238）。

- 密钥：pyotp 随机 Base32（20 字节，标准 TOTP）；
- 加密存储：TOTP 密钥先用 Fernet 加密（密文存 data/ai_export_permanent.json）；
  Fernet 主密钥本身用 Windows DPAPI（CryptProtectData）加密后落盘
  （data/ai_export_master.key），仅当前 Windows 用户可解密，任何明文主密钥
  都不落盘；
- 校验：pyotp.TOTP.verify(code, valid_window=1)（容忍 ±1 个 30 秒窗口，
  容忍时钟偏差）；
- 使用：访问固定接口时携带当前 6 位验证码 ?code=XXXXXX（30 秒轮换，
  链接泄露也看不到数据）。
"""
from __future__ import annotations

import base64
import ctypes
import ctypes.wintypes
import json
import os
import time
from typing import Optional

from cryptography.fernet import Fernet, InvalidToken

_MASTER_KEY_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "data", "ai_export_master.key"
)
_SECRETS_FILE = os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "..", "..", "..", "data", "ai_export_permanent.json"
)

# 允许的时钟偏差窗口数（每个 30 秒）。
# full 档（全量流水+已清仓+曲线+对账单）构建可能超过 30 秒，放宽到 ±2 个窗口
# （即约 90 秒内生成的验证码仍有效），避免大档生成期间验证码轮换导致 403。
_ALLOWED_WINDOW = 2


class _DATA_BLOB(ctypes.Structure):
    _fields_ = [("cbData", ctypes.wintypes.DWORD), ("pbData", ctypes.POINTER(ctypes.c_char))]


def _dpapi_protect(plain: bytes) -> bytes:
    """用 Windows DPAPI 加密（当前用户上下文）。"""
    if not plain:
        raise ValueError("empty plaintext")
    blob_in = _DATA_BLOB(len(plain), ctypes.cast(ctypes.create_string_buffer(plain), ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptProtectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError("CryptProtectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _dpapi_unprotect(cipher: bytes) -> bytes:
    if not cipher:
        raise ValueError("empty cipher")
    buf = ctypes.create_string_buffer(cipher)
    blob_in = _DATA_BLOB(len(cipher), ctypes.cast(buf, ctypes.POINTER(ctypes.c_char)))
    blob_out = _DATA_BLOB()
    if not ctypes.windll.crypt32.CryptUnprotectData(
        ctypes.byref(blob_in), None, None, None, None, 0, ctypes.byref(blob_out)
    ):
        raise OSError("CryptUnprotectData failed")
    try:
        return ctypes.string_at(blob_out.pbData, blob_out.cbData)
    finally:
        ctypes.windll.kernel32.LocalFree(blob_out.pbData)


def _get_master_key() -> bytes:
    """取 Fernet 主密钥：DPAPI 密文落盘，无文件则生成（全链路无明文落盘）。"""
    if os.path.exists(_MASTER_KEY_FILE):
        try:
            with open(_MASTER_KEY_FILE, "rb") as f:
                return _dpapi_unprotect(f.read())
        except Exception:  # noqa: BLE001  # 密文损坏/换用户 => 重新生成（旧 TOTP 密钥随之作废）
            pass
    key = Fernet.generate_key()
    os.makedirs(os.path.dirname(_MASTER_KEY_FILE), exist_ok=True)
    with open(_MASTER_KEY_FILE, "wb") as f:
        f.write(_dpapi_protect(key))
    return key


def _fernet() -> Fernet:
    return Fernet(_get_master_key())


def _load_secrets() -> dict:
    try:
        with open(_SECRETS_FILE, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data or {}
    except FileNotFoundError:
        return {}
    except Exception:  # noqa: BLE001
        return {}


def _save_secrets(data: dict) -> None:
    os.makedirs(os.path.dirname(_SECRETS_FILE), exist_ok=True)
    with open(_SECRETS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)


def generate_secret() -> str:
    """生成新的 TOTP Base32 密钥（单密钥模式：旧密钥自动作废）。返回明文密钥（仅显示一次）。"""
    import pyotp

    secret = pyotp.random_base32()
    cipher = _fernet().encrypt(secret.encode("utf-8"))
    data = _load_secrets()
    data["main"] = {"secret_cipher": cipher.decode("ascii"), "created_at": time.time()}
    _save_secrets(data)
    return secret


def _decrypt_secret() -> Optional[str]:
    data = _load_secrets()
    main = data.get("main")
    if not main or not main.get("secret_cipher"):
        return None
    try:
        plain = _fernet().decrypt(main["secret_cipher"].encode("ascii"))
        return plain.decode("utf-8")
    except (InvalidToken, Exception):  # noqa: BLE001
        return None


def verify_code(code: str) -> bool:
    """校验 TOTP 验证码（±1 个 30 秒窗口）。密钥缺失/解密失败一律 False。"""
    import pyotp

    secret = _decrypt_secret()
    if not secret or not code:
        return False
    try:
        return pyotp.TOTP(secret).verify(code.strip(), valid_window=_ALLOWED_WINDOW)
    except Exception:  # noqa: BLE001
        return False


def current_code() -> Optional[str]:
    """当前 30 秒窗口的 6 位验证码（服务端代算，供页面展示）。无密钥时 None。"""
    import pyotp

    secret = _decrypt_secret()
    if not secret:
        return None
    try:
        return pyotp.TOTP(secret).now()
    except Exception:  # noqa: BLE001
        return None


def provisioning_uri(secret: str) -> str:
    """otpauth:// URI，可直接导入 Google/Microsoft Authenticator。"""
    import pyotp

    return pyotp.TOTP(secret).provisioning_uri(name="DSA-AI-Export", issuer_name="daily-stock-analysis")


def secret_info() -> Optional[dict]:
    """固定密钥元信息（不含明文）。"""
    data = _load_secrets()
    main = data.get("main")
    if not main:
        return None
    return {"created_at": main.get("created_at")}


def revoke_secret() -> None:
    """作废固定密钥。"""
    data = _load_secrets()
    if "main" in data:
        data.pop("main", None)
        _save_secrets(data)
