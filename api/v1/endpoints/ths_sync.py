# -*- coding: utf-8 -*-
"""同花顺投资账本同步端点：扫码登录 + 拉取导入 + 导出文件同步。"""
from __future__ import annotations

import base64
import datetime as _dt
import logging
import os
import time as _time
from typing import Optional

from fastapi import APIRouter, File, Header, HTTPException, Query, UploadFile
from fastapi.responses import PlainTextResponse

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


@router.get("/stock-ledger")
def stock_ledger(
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
):
    """按股票维度汇总本地导入流水（买入/卖出/分红/税费），供个股流水视图。"""
    try:
        return _service().stock_ledger(start_date=start_date or "", end_date=end_date or "")
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("查询个股流水失败", exc)


@router.get("/holding-ledger")
def holding_ledger(
    start_date: Optional[str] = Query(None, description="开始日期 YYYY-MM-DD"),
    end_date: Optional[str] = Query(None, description="结束日期 YYYY-MM-DD"),
):
    """当前持仓表格数据：账本持仓（含持仓天数/成本/现价/盈亏）+ 本地流水明细。"""
    try:
        return _service().holding_ledger(start_date=start_date or "", end_date=end_date or "")
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("查询持仓流水失败", exc)


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

@router.get("/ai-export")
def ai_export(
    days: int = Query(90, description="交易流水回溯天数"),
    curve_days: int = Query(180, description="资产曲线天数"),
    scope: str = Query("full", pattern="^(core|compact|full)$", description="数据范围：core(仅总览+持仓+统计) / compact(+现金+最近20笔+曲线摘要) / full(完整)"),
    format: str = Query("json", description="json | text（text 返回可直接粘贴给 AI 的 Markdown）"),
):
    """AI 分析导出：一次性打包账户总览/持仓/现金流水/交易流水/资产曲线/对账单摘要。

    数据全部来自本地（账本导出文件导入 + 腾讯实时行情），无需账本登录态。
    """
    try:
        data = _service().build_ai_export(days=days, curve_days=curve_days, scope=scope)
        if format == "text":
            return PlainTextResponse(
                _render_ai_export_text(data, days, curve_days),
                media_type="text/plain; charset=utf-8",
            )
        return data
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("AI 数据导出失败", exc)

def _render_ai_export_text(data: dict, days: int = 90, curve_days: int = 180) -> str:
    """把 build_ai_export 结果渲染成 Markdown 文本（供 AI 直接阅读）。"""
    lines = []
    lines.append("# 持仓数据导出（AI 分析用）")
    lines.append("")
    lines.append("- 生成时间: %s" % (data.get("meta") or {}).get("generated_at", ""))
    lines.append("- 数据来源: %s" % (data.get("meta") or {}).get("source", ""))
    lines.append("- 账户: %s" % (data.get("meta") or {}).get("account", ""))
    lines.append("")
    ov = data.get("overview") or {}
    lines.append("## 账户总览")
    lines.append("")
    lines.append("| 指标 | 数值 |")
    lines.append("|---|---|")
    for k, v in ov.items():
        lines.append("| %s | %s |" % (k, v))
    lines.append("")
    pos = data.get("positions") or []
    lines.append("## 当前持仓（%d 只）" % len(pos))
    lines.append("")
    if pos:
        lines.append("| 代码 | 名称 | 数量 | 成本 | 现价 | 市值 | 持有盈亏 | 盈亏率%% | 当日盈亏 | 持仓天数 |")
        lines.append("|---|---|---|---|---|---|---|---|---|---|")
        for p in pos:
            lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                p.get("code", ""), p.get("name", ""), p.get("quantity", ""),
                p.get("cost", ""), p.get("price", ""), p.get("market_value", ""),
                p.get("hold_pnl", ""), p.get("hold_pnl_pct", ""),
                p.get("day_pnl", ""), p.get("hold_days", "")))
    lines.append("")
    lines.append("## 现金流水")
    lines.append("")
    for c in data.get("cash_ledger") or []:
        lines.append("- %s %s %s %s %s" % (c.get("event_date", ""), c.get("direction", ""),
                                           c.get("amount", ""), c.get("note", ""), c.get("account_name", "")))
    lines.append("")
    stats = data.get("recent_trade_stats") or {}
    is_full = (data.get("meta") or {}).get("trade_scope") == "full_history"
    lines.append("## %s交易统计" % ("全部历史" if is_full else "最近 %d 天" % days))
    lines.append("")
    for k, v in stats.items():
        lines.append("- %s: %s" % (k, v))
    lines.append("")
    tr = data.get("recent_trades") or []
    lines.append("## %s交易流水（%d 条，full 档为账户全量历史）" % ("全部历史" if is_full else "最近 %d 天" % days, len(tr)))
    lines.append("")
    if tr:
        lines.append("| 日期 | 类别 | 代码 | 名称 | 数量 | 价格 | 金额 | 费用 | 备注 |")
        lines.append("|---|---|---|---|---|---|---|---|---|")
        for r in tr:
            lines.append("| %s | %s | %s | %s | %s | %s | %s | %s | %s |" % (
                r.get("trade_date", ""), r.get("record_type", ""), r.get("code", ""),
                r.get("name", ""), r.get("quantity", ""), r.get("price", ""),
                r.get("amount", ""), r.get("fee", ""), r.get("note", "")))
    lines.append("")
    dv = data.get("dividends") or {}
    if dv:
        lines.append("## 分红记录（%d 笔，合计 %s 元）" % (dv.get("count", 0), dv.get("total", 0)))
        lines.append("")
        items = dv.get("items") or []
        if items:
            lines.append("| 日期 | 代码 | 名称 | 金额 | 备注 |")
            lines.append("|---|---|---|---|---|")
            for it in items:
                lines.append("| %s | %s | %s | %s | %s |" % (
                    it.get("date", ""), it.get("code", ""), it.get("name", ""),
                    it.get("amount", ""), it.get("note", "")))
            lines.append("")
    cf = data.get("cash_flows") or {}
    if cf:
        lines.append("## 出入金记录（%d 笔，入 %s / 出 %s / 净 %s 元）" % (
            cf.get("count", 0), cf.get("total_in", 0), cf.get("total_out", 0), cf.get("net", 0)))
        lines.append("")
        items = cf.get("items") or []
        if items:
            lines.append("| 日期 | 方向 | 金额 |")
            lines.append("|---|---|---|")
            for it in items:
                lines.append("| %s | %s | %s |" % (
                    it.get("date", ""), "入金" if it.get("direction") == "in" else "出金", it.get("amount", "")))
            lines.append("")
    cp = data.get("closed_positions") or {}
    if cp:
        cs = cp.get("stats") or {}
        lines.append("## 已清仓记录（%d 笔）" % cs.get("count", 0))
        lines.append("")
        for k, v in cs.items():
            lines.append("- %s: %s" % (k, v))
        lines.append("")
        citems = cp.get("items") or []
        if citems:
            lines.append("| 清仓日期 | 代码 | 名称 | 总盈亏 | 盈亏比% | 同期大盘% | 跑赢大盘 | 买入均价 |")
            lines.append("|---|---|---|---|---|---|---|---|")
            for it in citems:
                lines.append("| %s | %s | %s | %s | %s | %s | %s | %s |" % (
                    it.get("close_date", ""), it.get("symbol", ""), it.get("name", ""),
                    it.get("total_pnl", ""), it.get("pnl_ratio", ""), it.get("market_benchmark", ""),
                    it.get("beat_market", ""), it.get("avg_cost", "")))
            lines.append("")
    ec = (data.get("equity_curve") or {}).get("summary") or {}
    lines.append("## 资产曲线（最近 %d 天）" % curve_days)
    lines.append("")
    for k, v in ec.items():
        lines.append("- %s: %s" % (k, v))
    lines.append("")
    for sm in data.get("statement_months") or []:
        lines.append("## 对账单 %s" % sm.get("month", ""))
        lines.append("")
        for k, v in sm.items():
            lines.append("- %s: %s" % (k, v))
        lines.append("")
    sy = data.get("statement_year") or {}
    if sy:
        lines.append("## 年度对账单 %s" % sy.get("year", ""))
        lines.append("")
        for k, v in sy.items():
            if k == "months":
                continue
            lines.append("- %s: %s" % (k, v))
    return "\n".join(lines)


@router.post("/ai-export/token")
def create_ai_export_token(
    host: Optional[str] = Header(None, alias="Host"),
    ttl_hours: int = Query(1, ge=1, le=24, description="令牌有效期（小时），默认 1 小时"),
    grace_minutes: int = Query(10, ge=1, le=60, description="阅后宽限期（分钟）：首次读取后仍可重复读取的时长，默认 10 分钟"),
):
    """生成数据访问令牌（需登录）。返回分享 URL：外部 AI 首次读取后进入宽限期（默认 10 分钟），
    宽限期内可重复读取（抓取失败可重试）；未使用按 ttl_hours 自动过期。

    数据内容与 /ai-export 相同（账户总览/持仓/现金流水/交易流水/资产曲线/对账单）。
    """
    from src.services.ths_sync.ai_export_token import create_token

    ttl = ttl_hours * 3600
    grace = grace_minutes * 60
    token = create_token(ttl_seconds=ttl, grace_seconds=grace)
    host = (host or "127.0.0.1:8000").strip()
    scheme = "http"
    if host and not host.startswith("127.0.0.1") and not host.startswith("localhost") and not host.startswith("192.168.") and not host.startswith("10.") and not host.startswith("172."):
        scheme = "https"
    url = "%s://%s/api/v1/ths/ai-export/shared?token=%s" % (scheme, host, token)
    expires_ts = _time.time() + ttl
    return {
        "token": token,
        "url": url,
        "expires_at": _dt.datetime.fromtimestamp(expires_ts).strftime("%Y-%m-%d %H:%M:%S"),
        "expires_in_seconds": ttl,
        "read_grace_seconds": grace,
        "note": "首次读取后 %d 分钟内可重复读取（AI 抓取失败可重试）；超时未使用自动过期；服务重启后失效。" % grace_minutes,
    }


@router.post("/ai-export/token/permanent")
def create_ai_export_permanent_token(
    host: Optional[str] = Header(None, alias="Host"),
):
    """生成固定接口 TOTP 密钥（需登录）。

    - 返回 Base32 密钥（仅显示一次）与 otpauth:// URI：可导入 Google/
      Microsoft Authenticator，或由支持 TOTP 的 AI 自行计算验证码；
    - 访问固定接口需携带当前 6 位验证码 ?code=XXXXXX（30 秒轮换）；
    - 密钥加密持久化保存（DPAPI + Fernet），服务重启不失效；
    - 重新调用本接口即作废旧密钥（旧验证码立即失效）。
    """
    from src.services.ths_sync.ai_export_token import (
        create_permanent_token,
        permanent_provisioning_uri,
    )

    secret = create_permanent_token()
    host = (host or "127.0.0.1:8000").strip()
    scheme = "http"
    if host and not host.startswith("127.0.0.1") and not host.startswith("localhost") and not host.startswith("192.168.") and not host.startswith("10.") and not host.startswith("172."):
        scheme = "https"
    url = "%s://%s/api/v1/ths/ai-export/static" % (scheme, host)
    return {
        "url": url,
        "secret": secret,
        "otpauth_uri": permanent_provisioning_uri(secret),
        "scope": "full",
        "note": "Base32 密钥仅显示一次，请妥善保存（可导入 Google/Microsoft Authenticator 生成 6 位验证码）；固定接口不包含密钥，访问时拼接 ?code=当前验证码&format=text；重新生成会作废旧密钥。",
        "tips": "完整示例：%s?code=123456&format=text&days=90&curve_days=180" % url,
    }


@router.get("/ai-export/totp/current")
def ai_export_totp_current():
    """获取当前 30 秒窗口的 6 位验证码（需登录，服务端代算，供页面直接复制）。"""
    from src.services.ths_sync.ai_export_token import permanent_current_code, permanent_info

    if permanent_info() is None:
        raise HTTPException(status_code=404, detail={"error": "no_secret", "message": "尚未生成固定接口密钥"})
    return {"code": permanent_current_code(), "ttl_seconds": 30}


@router.get("/ai-export/static")
def ai_export_static(
    code: str = Query(..., description="TOTP 验证码（6 位，由固定密钥生成，30 秒轮换）"),
    days: int = Query(90, description="交易流水回溯天数"),
    curve_days: int = Query(180, description="资产曲线天数"),
    scope: str = Query("full", pattern="^(core|compact|full)$", description="数据范围：core(仅总览+持仓+统计) / compact(+现金+最近20笔+曲线摘要) / full(完整)"),
    format: str = Query("json", description="json | text（text 返回 Markdown）"),
):
    """免登录固定接口：凭当前 TOTP 验证码实时读取 AI 导出数据（每次访问实时生成最新数据）。

    验证码无效 / 过期 / 密钥已作废一律 403；密钥由管理员持有，只应提供给指定的 AI。
    """
    from src.services.ths_sync.ai_export_token import verify_permanent_token

    if not verify_permanent_token(code):
        raise HTTPException(
            status_code=403,
            detail={"error": "invalid_code", "message": "验证码无效或已过期，请使用当前 6 位验证码"},
        )
    try:
        data = _service().build_ai_export(days=days, curve_days=curve_days, scope=scope)
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("AI 数据导出失败", exc)
    if format == "text":
        return PlainTextResponse(
            _render_ai_export_text(data, days, curve_days),
            media_type="text/plain; charset=utf-8",
        )
    return data


@router.get("/ai-export/shared")
def ai_export_shared(
    token: str = Query(..., description="一次性令牌（由 /ai-export/token 生成）"),
    days: int = Query(90, description="交易流水回溯天数"),
    curve_days: int = Query(180, description="资产曲线天数"),
    scope: str = Query("full", pattern="^(core|compact|full)$", description="数据范围：core(仅总览+持仓+统计) / compact(+现金+最近20笔+曲线摘要) / full(完整)"),
    format: str = Query("json", description="json | text（text 返回 Markdown）"),
):
    """免登录共享访问：凭令牌读取 AI 导出数据，阅后宽限期。

    首次读取后进入宽限期（默认 10 分钟），宽限期内可重复读取（AI 抓取失败可重试）；
    超过宽限期 / 超时未用 / 无效令牌一律 403。
    """
    from src.services.ths_sync.ai_export_token import consume_token

    if not consume_token(token):
        raise HTTPException(
            status_code=403,
            detail={"error": "invalid_token", "message": "令牌无效、已使用或已过期，请重新生成"},
        )
    try:
        data = _service().build_ai_export(days=days, curve_days=curve_days, scope=scope)
    except Exception as exc:  # noqa: BLE001
        raise _internal_error("AI 数据导出失败", exc)
    if format == "text":
        return PlainTextResponse(
            _render_ai_export_text(data, days, curve_days),
            media_type="text/plain; charset=utf-8",
        )
    return data
