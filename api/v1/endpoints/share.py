# -*- coding: utf-8 -*-
"""独立访客分享页端点：固定口令（长期有效，可改）+ 展示股票白名单。

访问模型：
- 访客首次访问需输入固定口令换取 12 小时有效会话（share_session cookie）；
- 12 小时内访问无需重复输入；过期后需重新输入口令；
- 口令本身长期有效，只有管理员在配置里修改/清除时才变更。
- /share/ledger  ：访客数据接口（EXEMPT 放行，不依赖管理员登录态）
- /share/config  ：管理员配置接口（受 dsa_session 保护，仅本人可读写）
"""
from __future__ import annotations

import logging
from typing import List, Optional

from fastapi import APIRouter, HTTPException, Query, Request, Response
from pydantic import BaseModel

from api.v1.errors import api_error
from src.services.ths_sync.ths_sync_service import ThsSyncService

logger = logging.getLogger(__name__)

router = APIRouter()

_svc: Optional[ThsSyncService] = None

_SHARE_COOKIE = "share_session"
_SHARE_COOKIE_MAX_AGE = 12 * 3600  # 12 小时


def _service() -> ThsSyncService:
    global _svc
    if _svc is None:
        _svc = ThsSyncService()
    return _svc


def _internal_error(message: str, exc: Exception):
    logger.exception("share error: %s", exc)
    return api_error(500, "internal_error", message)


class ShareConfigRequest(BaseModel):
    """分享页配置保存请求体。仅传需要变更的字段。"""

    model_config = {"populate_by_name": True}

    enabled: Optional[bool] = None
    password: Optional[str] = None  # 新固定口令；空串表示清除口令；None 表示不修改
    symbols: Optional[List[str]] = None  # 展示股票白名单（按代码）


def _set_share_cookie(response: Response, token: str) -> None:
    response.set_cookie(
        key=_SHARE_COOKIE,
        value=token,
        httponly=True,
        samesite="lax",
        path="/",
        max_age=_SHARE_COOKIE_MAX_AGE,
    )


def _guest_authed(request: Request, password: Optional[str], response: Response) -> bool:
    """访客认证：口令换取/刷新 12h 会话，或已有有效会话。"""
    svc = _service()
    if password:
        if not svc.verify_share_password(password):
            return False
        token = svc.issue_share_session(hours=12)
        _set_share_cookie(response, token)
        return True
    token = request.cookies.get(_SHARE_COOKIE)
    return bool(token) and svc.verify_share_session(token)


@router.get("/ledger")
def share_ledger(
    request: Request,
    response: Response,
    password: Optional[str] = Query(None, description="分享固定口令（换取 12 小时访问）"),
):
    """访客分享页：口令/会话校验通过后返回白名单内股票的持仓与流水明细。

    只返回白名单股票，白名单外持仓（盈亏/金额/成本等）一律不下发。
    """
    try:
        if not _guest_authed(request, password, response):
            raise api_error(401, "share_unauthorized", "请先输入分享口令")
        svc = _service()
        config = svc.get_share_config()
        if not config.get("enabled"):
            raise api_error(403, "share_disabled", "分享功能未开启")
        whitelist = svc.share_whitelist_symbols()
        if not whitelist:
            return {"total": 0, "stocks": []}
        data = svc.holding_ledger()
        stocks = [st for st in data.get("stocks", []) if st.get("symbol") in whitelist]
        return {"total": len(stocks), "stocks": stocks}
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("加载分享数据失败", exc)


@router.get("/config")
def get_share_config():
    """管理员：读取分享页配置（不回显口令明文）。"""
    try:
        cfg = _service().get_share_config()
        return {
            "enabled": bool(cfg.get("enabled")),
            "symbols": cfg.get("symbols") or [],
            "hasPassword": bool(cfg.get("password_hash")),
            "updatedAt": cfg.get("updated_at") or "",
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("读取分享配置失败", exc)


@router.post("/config")
def save_share_config(body: ShareConfigRequest):
    """管理员：保存分享页配置（白名单/口令/开关），保存后即时生效。"""
    try:
        svc = _service()
        cfg = svc.get_share_config()
        if body.enabled is not None:
            cfg["enabled"] = bool(body.enabled)
        if body.password is not None:
            pwd = (body.password or "").strip()
            if not pwd:
                cfg["password_hash"] = ""
                cfg["password_salt"] = ""
            else:
                salt = __import__("uuid").uuid4().hex
                cfg["password_hash"] = svc._hash_share_password(pwd, salt)
                cfg["password_salt"] = salt
        if body.symbols is not None:
            seen: List[str] = []
            for raw in body.symbols:
                code = (raw or "").strip()
                if code and code not in seen:
                    seen.append(code)
            cfg["symbols"] = seen
        svc.save_share_config(cfg)
        return {
            "ok": True,
            "enabled": bool(cfg.get("enabled")),
            "symbols": cfg.get("symbols") or [],
            "hasPassword": bool(cfg.get("password_hash")),
            "updatedAt": cfg.get("updated_at") or "",
        }
    except HTTPException:
        raise
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("保存分享配置失败", exc)
