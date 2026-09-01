# -*- coding: utf-8 -*-
"""同花顺投资账本同步端点：扫码登录 + 拉取导入 + 导出文件同步。"""
from __future__ import annotations

import base64
import logging
import os
from typing import Optional

from fastapi import APIRouter, File, HTTPException, Query, UploadFile

from api.v1.errors import api_error
from src.services.ths_sync.ths_client import ThsLoginError
from src.services.ths_sync.ths_sync_service import ThsSyncService

logger = logging.getLogger(__name__)

router = APIRouter()

_svc: Optional[ThsSyncService] = None


def _service() -> ThsSyncService:
    global _svc
    if _svc is None:
        _svc = ThsSyncService()
    return _svc


def _internal_error(message: str, exc: Exception) -> HTTPException:
    logger.exception("ths sync error: %s", exc)
    return api_error(500, "internal_error", message)


@router.post("/qr-code")
def create_qrcode():
    """创建同花顺扫码二维码，返回 qrid 与图片（base64）。"""
    try:
        data = _service().create_qrcode()
        return {
            "qrid": data["qrid"],
            "qr_image": base64.b64encode(data["qr_image"]).decode("ascii"),
        }
    except ThsLoginError as exc:
        raise api_error(400, "ths_login_error", str(exc))
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("创建二维码失败", exc)


@router.post("/poll")
def poll_login(qrid: str = Query(..., description="qr-code 返回的 qrid"), timeout: float = Query(180.0)):
    """轮询扫码结果（阻塞），登录成功后返回 logged_in=true。"""
    try:
        result = _service().poll_login(qrid, timeout=timeout)
        return result
    except ThsLoginError as exc:
        raise api_error(400, "ths_login_error", str(exc))
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("轮询登录失败", exc)


@router.get("/status")
def get_status():
    """返回同花顺账本登录状态。"""
    try:
        return _service().get_status()
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("查询状态失败", exc)


@router.post("/logout")
def logout():
    """清除同花顺账本登录态。"""
    try:
        return _service().logout()
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("登出失败", exc)


@router.get("/trades")
def list_trades(
    start_date: Optional[str] = Query(None, description="开始日期 YYYYMMDD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYYMMDD"),
):
    """拉取账本全部账户交易流水（可选时间范围，不写入本地成本）。"""
    try:
        return _service().list_merged_trades(start_date=start_date, end_date=end_date)
    except ThsLoginError as exc:
        raise api_error(400, "ths_login_error", str(exc))
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("查询交易流水失败", exc)


@router.get("/import-records")
def list_import_records(
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
):
    """查询本地导入的账本导出流水（含逆回购/分红/银证转账等全部类别，无需登录）。"""
    try:
        return _service().list_local_import_records(start_date=start_date, end_date=end_date)
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("查询本地导入流水失败", exc)


@router.post("/sync")
def sync(import_asset: bool = Query(True, description="是否同步资产历史曲线")):
    """拉取账本汇总持仓与交易并导入本地账户。"""
    try:
        return _service().sync(import_asset=import_asset)
    except ThsLoginError as exc:
        raise api_error(400, "ths_login_error", str(exc))
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("同步失败", exc)


@router.get("/reconcile")
def reconcile():
    """对账：网页账本 vs 本地账户，判断账目是否一致（任一口径超阈值提示导出核对）。"""
    try:
        return _service().reconcile_web_vs_local()
    except ThsLoginError as exc:
        raise api_error(400, "ths_login_error", str(exc))
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("对账失败", exc)


# ----------------------------------------------------------------------
# 账本导出文件（汇总持仓.xlsx）同步：A 手动导入 / B 目录自动检测
# ----------------------------------------------------------------------
async def _save_upload(file: UploadFile, *, prefix: str) -> str:
    data_dir = os.path.dirname(_service().cookie_file)
    os.makedirs(data_dir, exist_ok=True)
    ext = os.path.splitext(file.filename or "汇总持仓.xlsx")[1] or ".xlsx"
    target = os.path.join(data_dir, f"{prefix}_{int(__import__('time').time())}{ext}")
    content = await file.read()
    with open(target, "wb") as fh:
        fh.write(content)
    return target


@router.post("/export-parse")
async def export_parse(file: UploadFile = File(...)):
    """上传账本导出的 汇总持仓.xlsx 并解析（不写库），返回持仓与交易统计。"""
    try:
        path = await _save_upload(file, prefix="ths_export_parse")
        return _service().parse_export_file(path)
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("解析导出文件失败", exc)


@router.post("/export-import")
async def export_import(file: UploadFile = File(...)):
    """上传账本导出的 汇总持仓.xlsx 并同步到本地账户。"""
    try:
        path = await _save_upload(file, prefix="ths_export_import")
        return _service().import_export_file(path)
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("导入导出文件失败", exc)


@router.get("/export-config")
def get_export_config():
    """获取导出文件目录配置与自动检测状态。"""
    try:
        return _service().get_export_config()
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("获取导出配置失败", exc)


@router.post("/export-config")
def save_export_config(
    directory: Optional[str] = Query(None, description="账本导出文件下载目录"),
    auto_sync: Optional[bool] = Query(None, description="是否开启定时自动检测同步"),
):
    """保存导出文件目录配置 / 自动检测开关。"""
    try:
        return _service().save_export_config(directory=directory, auto_sync=auto_sync)
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("保存导出配置失败", exc)


@router.post("/export-detect")
def export_detect():
    """检测配置下载目录中最新 汇总持仓.xlsx 并同步（方案B，指纹变化才同步）。"""
    try:
        svc = _service()
        check = svc.should_sync_export()
        if not check.get("detected") or not check.get("latest"):
            return {"detected": False, "message": "未在配置目录找到 汇总持仓.xlsx"}
        latest = check["latest"]
        if not check.get("changed"):
            config = svc.get_export_config()
            return {
                "detected": True,
                "changed": False,
                "message": "目录中的导出文件未发生变化，已跳过重复同步",
                "last_file": os.path.basename(latest),
                "last_synced_at": config.get("last_synced_at", ""),
                # 兼容前端「同步结果卡片」渲染所需字段（避免 rebuilt.join 等空值崩溃）
                "position_count": 0,
                "positions_applied": 0,
                "rebuilt": [],
                "rebuild_errors": [],
                "cash_import": {"adjusted": False, "direction": "", "amount": 0},
                "total_cash": None,
                "funds_skipped": [],
                "trade_stats": {
                    "trade_buy": 0,
                    "trade_sell": 0,
                    "cash_in": 0,
                    "cash_out": 0,
                    "other": 0,
                    "first_date": None,
                    "last_date": None,
                },
                "cash_in_total": 0.0,
                "cash_out_total": 0.0,
                "market_value": 0.0,
                "import_record_count": 0,
            }
        result = svc.import_export_file(latest)
        result["detected"] = True
        result["changed"] = True
        result["file_path"] = latest
        svc.record_export_synced(latest)
        config = svc.get_export_config()
        result["last_synced_at"] = config.get("last_synced_at", "")
        return result
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("自动检测同步失败", exc)
