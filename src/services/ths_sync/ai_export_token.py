# -*- coding: utf-8 -*-
"""AI 数据导出一次性令牌（阅后即焚）。

- 生成：登录态调用 token 接口生成，有效期默认 1 小时（可配 1-24 小时）；
- 使用：外部 AI 携带 token 访问 shared 接口，成功读取后立即作废（阅后即焚）；
- 过期：超时未用自动失效；服务重启后内存清空（令牌全部失效，更安全）。
"""

import secrets
import threading
import time

_TOKEN_TTL_SECONDS = 3600  # 默认 1 小时

_tokens: dict[str, dict] = {}  # token -> {"created_at": float, "expires_at": float}
_lock = threading.Lock()


def create_token(ttl_seconds: int = _TOKEN_TTL_SECONDS) -> str:
    """生成一次性令牌。ttl_seconds 不得小于 60 秒。"""
    if ttl_seconds < 60:
        ttl_seconds = 60
    with _lock:
        _purge_locked()
        token = secrets.token_urlsafe(32)
        now = time.time()
        _tokens[token] = {"created_at": now, "expires_at": now + ttl_seconds}
        return token


def _purge_locked() -> None:
    """删除所有已过期令牌（调用方需持有锁）。"""
    now = time.time()
    expired = [t for t, info in _tokens.items() if info["expires_at"] <= now]
    for t in expired:
        _tokens.pop(t, None)


def consume_token(token: str) -> bool:
    """消费令牌：有效则立即删除（阅后即焚）并返回 True，否则返回 False。"""
    if not token or not isinstance(token, str):
        return False
    with _lock:
        _purge_locked()
        info = _tokens.pop(token, None)
        if info is None:
            return False
        if info["expires_at"] <= time.time():
            return False
        return True


def token_info(token: str):
    """查询令牌信息（不消费、不作废）。无效或过期返回 None。"""
    if not token:
        return None
    with _lock:
        _purge_locked()
        info = _tokens.get(token)
        if info is None or info["expires_at"] <= time.time():
            return None
        return dict(info)


def active_count() -> int:
    """当前有效令牌数量（调试用）。"""
    with _lock:
        _purge_locked()
        return len(_tokens)
