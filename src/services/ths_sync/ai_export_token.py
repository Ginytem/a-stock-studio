# -*- coding: utf-8 -*-
"""AI 数据导出令牌。

一次性令牌（阅后宽限期）：
- 生成：登录态调用 token 接口生成，有效期默认 1 小时（可配 1-24 小时）；
- 使用：外部 AI 携带 token 访问 shared 接口，成功读取后进入宽限期
  （默认 10 分钟，可配 1-60 分钟），宽限期内可重复读取（AI 抓取失败可重试）；
  超过宽限期或超时未用即失效；
- 过期：超时未用自动失效；服务重启后内存清空（令牌全部失效，更安全）。

固定接口密钥（长期有效，持久化）：
- 生成：登录态调用 token/permanent 接口生成，密钥只显示一次；
- 使用：外部 AI 携带 key 访问 static 接口，每次访问实时生成最新数据；
- 持久化：密钥哈希写入 data/ai_export_permanent.json，服务重启不失效；
- 作废：重新生成即作废旧密钥（单密钥模式）；也可通过 revoke 主动作废。
"""

import secrets
import threading
import time

_TOKEN_TTL_SECONDS = 3600  # 默认 1 小时（未读取时的最大存活时长）
_TOKEN_GRACE_SECONDS = 600  # 阅后宽限期：首次成功读取后 10 分钟内仍可重复读取

_tokens: dict[str, dict] = {}  # token -> {"created_at": float, "expires_at": float, "consumed_at": float|None}
_lock = threading.Lock()


def create_token(ttl_seconds: int = _TOKEN_TTL_SECONDS, grace_seconds: int = _TOKEN_GRACE_SECONDS) -> str:
    """生成一次性令牌。ttl_seconds 不得小于 60 秒；grace_seconds 不得小于 60 秒。"""
    if ttl_seconds < 60:
        ttl_seconds = 60
    if grace_seconds < 60:
        grace_seconds = 60
    with _lock:
        _purge_locked()
        token = secrets.token_urlsafe(32)
        now = time.time()
        _tokens[token] = {
            "created_at": now,
            "expires_at": now + ttl_seconds,
            "consumed_at": None,
            "grace_seconds": grace_seconds,
        }
        return token


def _purge_locked() -> None:
    """删除所有已失效令牌（调用方需持有锁）：超时未用，或已读取且超过阅后宽限期。"""
    now = time.time()
    expired = [
        t
        for t, info in _tokens.items()
        if info["expires_at"] <= now
        or (info.get("consumed_at") is not None and now - info["consumed_at"] > info.get("grace_seconds", _TOKEN_GRACE_SECONDS))
    ]
    for t in expired:
        _tokens.pop(t, None)


def consume_token(token: str) -> bool:
    """校验令牌并标记首次读取。

    - 首次读取：记录 consumed_at 并返回 True（此后进入阅后宽限期）；
    - 已读取：宽限期内返回 True（可重复读取），超过宽限期返回 False 并清除；
    - 无效 / 超时未用 / 超宽限期：返回 False。
    """
    if not token or not isinstance(token, str):
        return False
    with _lock:
        _purge_locked()
        info = _tokens.get(token)
        if info is None:
            return False
        if info["expires_at"] <= time.time():
            _tokens.pop(token, None)
            return False
        consumed_at = info.get("consumed_at")
        if consumed_at is not None:
            if time.time() - consumed_at > info.get("grace_seconds", _TOKEN_GRACE_SECONDS):
                _tokens.pop(token, None)
                return False
            return True
        info["consumed_at"] = time.time()
        return True


def token_info(token: str):
    """查询令牌信息（不消费、不作废）。无效、过期或超宽限期返回 None。"""
    if not token:
        return None
    with _lock:
        _purge_locked()
        info = _tokens.get(token)
        if info is None or info["expires_at"] <= time.time():
            return None
        consumed_at = info.get("consumed_at")
        if consumed_at is not None and time.time() - consumed_at > info.get("grace_seconds", _TOKEN_GRACE_SECONDS):
            return None
        return dict(info)


def active_count() -> int:
    """当前有效令牌数量（调试用）。"""
    with _lock:
        _purge_locked()
        return len(_tokens)


# ---------------------------------------------------------------------------
# 固定接口密钥（TOTP，RFC 6238，加密持久化到 data/ 下）
# 实现见 ai_totp.py（DPAPI 主密钥 + Fernet 加密 TOTP 密钥，全密文落盘）
# ---------------------------------------------------------------------------


def create_permanent_token() -> str:
    """生成新的 TOTP Base32 密钥（单密钥模式：旧密钥自动作废）。返回明文密钥（仅显示一次）。"""
    from src.services.ths_sync import ai_totp

    return ai_totp.generate_secret()


def verify_permanent_token(code: str) -> bool:
    """校验 TOTP 验证码（±1 个 30 秒窗口）。"""
    from src.services.ths_sync import ai_totp

    return ai_totp.verify_code(code)


def revoke_permanent_token() -> None:
    """作废固定密钥。"""
    from src.services.ths_sync import ai_totp

    ai_totp.revoke_secret()


def permanent_info():
    """查询固定密钥的元信息（不含密钥本身）。无密钥时返回 None。"""
    from src.services.ths_sync import ai_totp

    return ai_totp.secret_info()


def permanent_current_code() -> str:
    """当前 30 秒窗口的 6 位验证码（服务端代算）。无密钥返回空串。"""
    from src.services.ths_sync import ai_totp

    return ai_totp.current_code() or ""


def permanent_provisioning_uri(secret: str) -> str:
    from src.services.ths_sync import ai_totp

    return ai_totp.provisioning_uri(secret)
