# -*- coding: utf-8 -*-
"""Portfolio endpoints (P0 core account + snapshot workflow)."""

from __future__ import annotations

import logging
from datetime import date
from typing import Optional

from fastapi import APIRouter, File, Form, HTTPException, Query, UploadFile
from fastapi.responses import JSONResponse

from api.v1.errors import api_error
from api.v1.schemas.analysis import DuplicateTaskErrorResponse, TaskAccepted
from api.v1.schemas.common import ErrorResponse
from api.v1.schemas.portfolio import (
    PortfolioAccountCreateRequest,
    PortfolioAccountItem,
    PortfolioAccountListResponse,
    PortfolioAccountUpdateRequest,
    PortfolioCashLedgerListResponse,
    PortfolioCashLedgerCreateRequest,
    PortfolioCorporateActionListResponse,
    PortfolioCorporateActionCreateRequest,
    PortfolioDeleteResponse,
    PortfolioEventCreatedResponse,
    PortfolioFxRefreshResponse,
    PortfolioImportBrokerListResponse,
    PortfolioImportCommitResponse,
    PortfolioImportParseResponse,
    PortfolioImportTradeItem,
    PortfolioPositionAnalysisRequest,
    PortfolioRiskResponse,
    PortfolioSnapshotResponse,
    PortfolioTradeListResponse,
    PortfolioTradeCreateRequest,
)
from src.services.task_queue import get_task_queue
from src.services.portfolio_import_service import PortfolioImportService
from src.services.portfolio_risk_service import PortfolioRiskService
from src.services.portfolio_service import (
    PortfolioBusyError,
    PortfolioConflictError,
    PortfolioOversellError,
    PortfolioService,
)

logger = logging.getLogger(__name__)

router = APIRouter()


def _bad_request(exc: Exception) -> HTTPException:
    return api_error(400, "validation_error", str(exc))


def _internal_error(message: str, exc: Exception) -> HTTPException:
    logger.error(f"{message}: {exc}", exc_info=True)
    return api_error(500, "internal_error", f"{message}: {str(exc)}")


def _conflict_error(*, error: str, message: str) -> HTTPException:
    return api_error(409, error, message)


def _serialize_import_record(item: dict) -> PortfolioImportTradeItem:
    payload = dict(item)
    trade_date = payload.get("trade_date")
    if isinstance(trade_date, date):
        payload["trade_date"] = trade_date.isoformat()
    else:
        payload["trade_date"] = str(trade_date)
    return PortfolioImportTradeItem(**payload)


@router.post(
    "/accounts",
    response_model=PortfolioAccountItem,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Create portfolio account",
)
def create_account(request: PortfolioAccountCreateRequest) -> PortfolioAccountItem:
    service = PortfolioService()
    try:
        row = service.create_account(
            name=request.name,
            broker=request.broker,
            market=request.market,
            base_currency=request.base_currency,
            owner_id=request.owner_id,
        )
        return PortfolioAccountItem(**row)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Create account failed", exc)


@router.get(
    "/accounts",
    response_model=PortfolioAccountListResponse,
    responses={500: {"model": ErrorResponse}},
    summary="List portfolio accounts",
)
def list_accounts(
    include_inactive: bool = Query(False, description="Whether to include inactive accounts"),
) -> PortfolioAccountListResponse:
    service = PortfolioService()
    try:
        rows = service.list_accounts(include_inactive=include_inactive)
        return PortfolioAccountListResponse(accounts=[PortfolioAccountItem(**item) for item in rows])
    except Exception as exc:
        raise _internal_error("List accounts failed", exc)


@router.put(
    "/accounts/{account_id}",
    response_model=PortfolioAccountItem,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Update portfolio account",
)
def update_account(account_id: int, request: PortfolioAccountUpdateRequest) -> PortfolioAccountItem:
    service = PortfolioService()
    try:
        updated = service.update_account(
            account_id,
            name=request.name,
            broker=request.broker,
            market=request.market,
            base_currency=request.base_currency,
            owner_id=request.owner_id,
            is_active=request.is_active,
        )
        if updated is None:
            raise api_error(404, "not_found", f"Account not found: {account_id}")
        return PortfolioAccountItem(**updated)
    except HTTPException:
        raise
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Update account failed", exc)


@router.delete(
    "/accounts/{account_id}",
    responses={404: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Deactivate portfolio account",
)
def delete_account(account_id: int):
    service = PortfolioService()
    try:
        ok = service.deactivate_account(account_id)
        if not ok:
            raise api_error(404, "not_found", f"Account not found: {account_id}")
        return {"deleted": 1}
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Deactivate account failed", exc)


@router.post(
    "/trades",
    response_model=PortfolioEventCreatedResponse,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Record trade event",
)
def create_trade(request: PortfolioTradeCreateRequest) -> PortfolioEventCreatedResponse:
    service = PortfolioService()
    try:
        data = service.record_trade(
            account_id=request.account_id,
            symbol=request.symbol,
            trade_date=request.trade_date,
            side=request.side,
            quantity=request.quantity,
            price=request.price,
            fee=request.fee,
            tax=request.tax,
            market=request.market,
            currency=request.currency,
            trade_uid=request.trade_uid,
            note=request.note,
        )
        return PortfolioEventCreatedResponse(**data)
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except PortfolioOversellError as exc:
        raise _conflict_error(error="portfolio_oversell", message=str(exc))
    except PortfolioConflictError as exc:
        raise _conflict_error(error="conflict", message=str(exc))
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Create trade failed", exc)


@router.get(
    "/trades",
    response_model=PortfolioTradeListResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="List trade events",
)
def list_trades(
    account_id: Optional[int] = Query(None, description="Optional account id"),
    date_from: Optional[date] = Query(None, description="Trade date from"),
    date_to: Optional[date] = Query(None, description="Trade date to"),
    symbol: Optional[str] = Query(None, description="Optional stock symbol filter"),
    side: Optional[str] = Query(None, description="Optional side filter: buy/sell"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PortfolioTradeListResponse:
    service = PortfolioService()
    try:
        data = service.list_trade_events(
            account_id=account_id,
            date_from=date_from,
            date_to=date_to,
            symbol=symbol,
            side=side,
            page=page,
            page_size=page_size,
        )
        return PortfolioTradeListResponse(**data)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("List trade events failed", exc)


@router.delete(
    "/trades/{trade_id}",
    response_model=PortfolioDeleteResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Delete trade event",
)
def delete_trade(trade_id: int) -> PortfolioDeleteResponse:
    service = PortfolioService()
    try:
        ok = service.delete_trade_event(trade_id)
        if not ok:
            raise api_error(404, "not_found", f"Trade not found: {trade_id}")
        return PortfolioDeleteResponse(deleted=1)
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Delete trade event failed", exc)


@router.post(
    "/cash-ledger",
    response_model=PortfolioEventCreatedResponse,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Record cash event",
)
def create_cash_ledger(request: PortfolioCashLedgerCreateRequest) -> PortfolioEventCreatedResponse:
    service = PortfolioService()
    try:
        data = service.record_cash_ledger(
            account_id=request.account_id,
            event_date=request.event_date,
            direction=request.direction,
            amount=request.amount,
            currency=request.currency,
            note=request.note,
        )
        return PortfolioEventCreatedResponse(**data)
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Create cash ledger event failed", exc)


@router.get(
    "/cash-ledger",
    response_model=PortfolioCashLedgerListResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="List cash ledger events",
)
def list_cash_ledger(
    account_id: Optional[int] = Query(None, description="Optional account id"),
    date_from: Optional[date] = Query(None, description="Cash event date from"),
    date_to: Optional[date] = Query(None, description="Cash event date to"),
    direction: Optional[str] = Query(None, description="Optional direction filter: in/out"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PortfolioCashLedgerListResponse:
    service = PortfolioService()
    try:
        data = service.list_cash_ledger_events(
            account_id=account_id,
            date_from=date_from,
            date_to=date_to,
            direction=direction,
            page=page,
            page_size=page_size,
        )
        return PortfolioCashLedgerListResponse(**data)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("List cash ledger events failed", exc)


@router.delete(
    "/cash-ledger/{entry_id}",
    response_model=PortfolioDeleteResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Delete cash ledger event",
)
def delete_cash_ledger(entry_id: int) -> PortfolioDeleteResponse:
    service = PortfolioService()
    try:
        ok = service.delete_cash_ledger_event(entry_id)
        if not ok:
            raise api_error(404, "not_found", f"Cash ledger entry not found: {entry_id}")
        return PortfolioDeleteResponse(deleted=1)
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Delete cash ledger event failed", exc)


@router.post(
    "/corporate-actions",
    response_model=PortfolioEventCreatedResponse,
    responses={400: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Record corporate action event",
)
def create_corporate_action(request: PortfolioCorporateActionCreateRequest) -> PortfolioEventCreatedResponse:
    service = PortfolioService()
    try:
        data = service.record_corporate_action(
            account_id=request.account_id,
            symbol=request.symbol,
            effective_date=request.effective_date,
            action_type=request.action_type,
            market=request.market,
            currency=request.currency,
            cash_dividend_per_share=request.cash_dividend_per_share,
            split_ratio=request.split_ratio,
            note=request.note,
        )
        return PortfolioEventCreatedResponse(**data)
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Create corporate action event failed", exc)


@router.get(
    "/corporate-actions",
    response_model=PortfolioCorporateActionListResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="List corporate action events",
)
def list_corporate_actions(
    account_id: Optional[int] = Query(None, description="Optional account id"),
    date_from: Optional[date] = Query(None, description="Corporate action effective date from"),
    date_to: Optional[date] = Query(None, description="Corporate action effective date to"),
    symbol: Optional[str] = Query(None, description="Optional stock symbol filter"),
    action_type: Optional[str] = Query(None, description="Optional action type filter"),
    page: int = Query(1, ge=1),
    page_size: int = Query(20, ge=1, le=100),
) -> PortfolioCorporateActionListResponse:
    service = PortfolioService()
    try:
        data = service.list_corporate_action_events(
            account_id=account_id,
            date_from=date_from,
            date_to=date_to,
            symbol=symbol,
            action_type=action_type,
            page=page,
            page_size=page_size,
        )
        return PortfolioCorporateActionListResponse(**data)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("List corporate action events failed", exc)


@router.delete(
    "/corporate-actions/{action_id}",
    response_model=PortfolioDeleteResponse,
    responses={404: {"model": ErrorResponse}, 409: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Delete corporate action event",
)
def delete_corporate_action(action_id: int) -> PortfolioDeleteResponse:
    service = PortfolioService()
    try:
        ok = service.delete_corporate_action_event(action_id)
        if not ok:
            raise api_error(404, "not_found", f"Corporate action not found: {action_id}")
        return PortfolioDeleteResponse(deleted=1)
    except PortfolioBusyError as exc:
        raise _conflict_error(error="portfolio_busy", message=str(exc))
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Delete corporate action event failed", exc)


@router.get(
    "/snapshot",
    response_model=PortfolioSnapshotResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Get portfolio snapshot",
)
def get_snapshot(
    account_id: Optional[int] = Query(None, description="Optional account id, default returns all accounts"),
    as_of: Optional[date] = Query(None, description="Snapshot date, default today"),
    cost_method: str = Query("fifo", description="Cost method: fifo or avg"),
    include_realtime: bool = Query(
        True,
        description="Whether today's snapshot should try realtime quotes before historical close fallback",
    ),
) -> PortfolioSnapshotResponse:
    service = PortfolioService()
    try:
        data = service.get_portfolio_snapshot(
            account_id=account_id,
            as_of=as_of,
            cost_method=cost_method,
            include_realtime=include_realtime,
        )
        return PortfolioSnapshotResponse(**data)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Get snapshot failed", exc)


@router.get(
    "/statement",
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="月度对账单",
    description="按月份聚合买入/卖出/资金流水/分红，并给出期初期末资产与月收益率",
)
def get_monthly_statement(
    month: str = Query(..., description="月份 YYYY-MM"),
    account_id: Optional[int] = Query(None, description="账户 ID，缺省为全部账户"),
    cost_method: str = Query("fifo", description="成本口径 fifo/avg"),
    use_ths: bool = Query(False, description="使用同花顺账本交易数据生成对账单（含国债逆回购等全部流水）"),
) -> dict:
    if use_ths:
        from src.services.ths_sync.ths_client import ThsLoginError
        from src.services.ths_sync.ths_sync_service import ThsSyncService

        try:
            return ThsSyncService().build_statement(month=month, account_id=account_id, cost_method=cost_method)
        except ThsLoginError as exc:
            raise api_error(400, "ths_login_error", str(exc))
        except HTTPException:
            raise
        except Exception as exc:
            raise _internal_error("Build THS statement failed", exc)
    service = PortfolioService()
    try:
        return service.build_monthly_statement(
            month=month,
            account_id=account_id,
            cost_method=cost_method,
        )
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Build monthly statement failed", exc)


@router.get(
    "/equity-curve",
    responses={500: {"model": ErrorResponse}},
    summary="资产曲线",
    description="按日快照返回总资产/市值/现金序列、区间收益率与最大回撤",
)
def get_equity_curve(
    days: int = Query(180, ge=0, le=7300, description="回溯天数，0 表示全部历史"),
    account_id: Optional[int] = Query(None, description="账户 ID，缺省为全部账户"),
    cost_method: str = Query("fifo", description="成本口径 fifo/avg"),
) -> dict:
    service = PortfolioService()
    try:
        return service.build_equity_curve(
            days=days,
            account_id=account_id,
            cost_method=cost_method,
        )
    except Exception as exc:
        raise _internal_error("Build equity curve failed", exc)


@router.post(
    "/positions/{symbol}/analysis",
    status_code=202,
    response_model=TaskAccepted,
    responses={400: {"model": ErrorResponse}, 404: {"model": ErrorResponse}, 409: {"model": DuplicateTaskErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Submit manual analysis for a held portfolio position",
)
def analyze_position(symbol: str, request: PortfolioPositionAnalysisRequest) -> TaskAccepted | JSONResponse:
    service = PortfolioService()
    try:
        context = _resolve_position_analysis_context(service, symbol=symbol, account_id=request.account_id)
    except HTTPException:
        raise
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Resolve portfolio position failed", exc)

    # 当日去重缓存：非 force 且当日已有分析结果时直接复用，避免重复消耗 LLM 额度
    if not request.force:
        try:
            from src.storage import DatabaseManager

            db = DatabaseManager()
            recent = db.get_analysis_history(code=context["symbol"], days=1, limit=1)
            if recent:
                latest = recent[0]
                created = getattr(latest, "created_at", None)
                created_text = created.strftime("%H:%M") if hasattr(created, "strftime") else "今日"
                return TaskAccepted(
                    task_id=f"reuse:{getattr(latest, 'id', '')}",
                    trace_id=getattr(latest, "query_id", None),
                    status="reused",
                    message=(
                        f"{context['symbol']} 今日 {created_text} 已有分析结果，已复用（未重复消耗额度）；"
                        "如需强制重新分析请勾选 force 后重试。"
                    ),
                    analysis_phase=request.analysis_phase,
                )
        except Exception as exc:
            logger.info("持仓分析当日去重检查失败，继续正常提交: %s", exc)

    queue = get_task_queue()
    accepted, duplicates = queue.submit_tasks_batch(
        [context["symbol"]],
        stock_name=None,
        original_query=context["symbol"],
        selection_source="manual",
        query_source="portfolio",
        portfolio_context=context,
        report_type="detailed",
        analysis_phase=request.analysis_phase,
        force_refresh=bool(request.force),
        notify=True,
    )
    if duplicates:
        dup = duplicates[0]
        error_response = DuplicateTaskErrorResponse(
            error="duplicate_task",
            message=str(dup),
            stock_code=dup.stock_code,
            existing_task_id=dup.existing_task_id,
        )
        return JSONResponse(status_code=409, content=error_response.model_dump())
    task = accepted[0]
    response = TaskAccepted(
        task_id=task.task_id,
        trace_id=task.trace_id or task.task_id,
        status="pending",
        message=f"分析任务已加入队列: {task.stock_code}",
        analysis_phase=task.analysis_phase,
    )
    return response


def _resolve_position_analysis_context(
    service: PortfolioService,
    *,
    symbol: str,
    account_id: Optional[int],
) -> dict:
    target = service._normalize_symbol_for_position(symbol)
    if not target:
        raise ValueError("symbol must not be empty")

    snapshot = service.get_portfolio_snapshot(account_id=account_id, cost_method="fifo")
    matches = []
    for account in snapshot.get("accounts") or []:
        for position in account.get("positions") or []:
            position_symbol = service._normalize_symbol_for_position(
                str(position.get("symbol") or "")
            )
            if position_symbol != target:
                continue
            try:
                quantity = float(position.get("quantity") or 0)
            except (TypeError, ValueError):
                quantity = 0.0
            if quantity <= 0:
                continue
            matches.append((account, position, position_symbol))

    if not matches:
        raise api_error(404, "not_found", f"No non-zero portfolio position for {target}")
    if account_id is None:
        account_ids = {
            int(account.get("account_id"))
            for account, _, _ in matches
            if account.get("account_id") is not None
        }
        if len(account_ids) > 1:
            raise api_error(
                400,
                "ambiguous_position_account",
                f"{target} is held in multiple accounts; pass account_id",
            )

    account, position, position_symbol = matches[0]
    return {
        "account_id": account.get("account_id"),
        "account_name": account.get("account_name"),
        "symbol": position_symbol or target,
        "market": position.get("market"),
        "currency": position.get("currency"),
        "quantity": position.get("quantity"),
        "avg_cost": position.get("avg_cost"),
        "total_cost": position.get("total_cost"),
        "unrealized_pnl_base": position.get("unrealized_pnl_base"),
        "unrealized_pnl_pct": position.get("unrealized_pnl_pct"),
        "price_source": position.get("price_source"),
        "price_provider": position.get("price_provider"),
        "price_date": position.get("price_date"),
        "price_stale": bool(position.get("price_stale")),
        "price_available": bool(position.get("price_available", True)),
        "cost_method": snapshot.get("cost_method") or "fifo",
    }


@router.post(
    "/imports/csv/parse",
    response_model=PortfolioImportParseResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Parse broker CSV into normalized trade records",
)
def parse_csv_import(
    broker: str = Form(..., description="Broker id: huatai/citic/cmb"),
    file: UploadFile = File(...),
) -> PortfolioImportParseResponse:
    importer = PortfolioImportService()
    try:
        content = file.file.read()
        parsed = importer.parse_trade_csv(broker=broker, content=content)
        return PortfolioImportParseResponse(
            broker=parsed["broker"],
            record_count=parsed["record_count"],
            skipped_count=parsed["skipped_count"],
            error_count=parsed["error_count"],
            records=[_serialize_import_record(item) for item in parsed.get("records", [])],
            errors=list(parsed.get("errors", [])),
        )
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Parse CSV import failed", exc)


@router.get(
    "/imports/csv/brokers",
    response_model=PortfolioImportBrokerListResponse,
    responses={500: {"model": ErrorResponse}},
    summary="List supported broker CSV parsers",
)
def list_csv_brokers() -> PortfolioImportBrokerListResponse:
    importer = PortfolioImportService()
    try:
        return PortfolioImportBrokerListResponse(brokers=importer.list_supported_brokers())
    except Exception as exc:
        raise _internal_error("List CSV brokers failed", exc)


@router.post(
    "/imports/csv/commit",
    response_model=PortfolioImportCommitResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Parse and commit broker CSV with dedup",
)
def commit_csv_import(
    account_id: int = Form(...),
    broker: str = Form(..., description="Broker id: huatai/citic/cmb"),
    dry_run: bool = Form(False),
    file: UploadFile = File(...),
) -> PortfolioImportCommitResponse:
    importer = PortfolioImportService()
    try:
        content = file.file.read()
        parsed = importer.parse_trade_csv(broker=broker, content=content)
        result = importer.commit_trade_records(
            account_id=account_id,
            broker=parsed["broker"],
            records=list(parsed.get("records", [])),
            dry_run=dry_run,
        )
        return PortfolioImportCommitResponse(**result)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Commit CSV import failed", exc)


@router.post(
    "/fx/refresh",
    response_model=PortfolioFxRefreshResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Refresh FX cache online with stale fallback",
)
def refresh_fx_rates(
    account_id: Optional[int] = Query(None, description="Optional account id"),
    as_of: Optional[date] = Query(None, description="Rate date, default today"),
) -> PortfolioFxRefreshResponse:
    service = PortfolioService()
    try:
        data = service.refresh_fx_rates(account_id=account_id, as_of=as_of)
        return PortfolioFxRefreshResponse(**data)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Refresh FX rates failed", exc)


@router.get(
    "/risk",
    response_model=PortfolioRiskResponse,
    responses={400: {"model": ErrorResponse}, 500: {"model": ErrorResponse}},
    summary="Get portfolio risk report",
)
def get_risk_report(
    account_id: Optional[int] = Query(None, description="Optional account id"),
    as_of: Optional[date] = Query(None, description="Risk report date, default today"),
    cost_method: str = Query("fifo", description="Cost method: fifo or avg"),
    include_realtime: bool = Query(
        True,
        description="Whether today's risk snapshot should try realtime quotes before historical close fallback",
    ),
) -> PortfolioRiskResponse:
    service = PortfolioRiskService()
    try:
        data = service.get_risk_report(
            account_id=account_id,
            as_of=as_of,
            cost_method=cost_method,
            include_realtime=include_realtime,
        )
        return PortfolioRiskResponse(**data)
    except ValueError as exc:
        raise _bad_request(exc)
    except Exception as exc:
        raise _internal_error("Get risk report failed", exc)


@router.get(
    "/share-image",
    responses={
        200: {"content": {"image/png": {}}, "description": "PNG share card"},
        400: {"model": ErrorResponse},
        500: {"model": ErrorResponse},
    },
    summary="Generate portfolio share card PNG (today P&L + sector allocation)",
)
def get_share_image(
    account_id: Optional[int] = Query(None, description="Optional account id"),
    as_of: Optional[date] = Query(None, description="Snapshot date, default today"),
    cost_method: str = Query("fifo", description="Cost method: fifo or avg"),
    include_realtime: bool = Query(
        True,
        description="Whether today's snapshot should try realtime quotes before historical close fallback",
    ),
    show_pnl_amount: bool = Query(True, description="Whether to render today's P&L amount on the share card"),
    show_pnl_pct: bool = Query(True, description="Whether to render today's P&L percent on the share card"),
    show_equity: bool = Query(True, description="Whether to render total equity on the share card"),
):
    """Build a compliance-safe share card: today's P&L, 7-day trend and sector allocation only."""
    try:
        from fastapi.responses import Response

        from src.portfolio_share import generate_portfolio_share_image

        portfolio_service = PortfolioService()
        snapshot = portfolio_service.get_portfolio_snapshot(
            account_id=account_id,
            as_of=as_of,
            cost_method=cost_method,
            include_realtime=include_realtime,
        )

        risk_service = PortfolioRiskService(portfolio_service=portfolio_service)
        risk_data = risk_service.get_risk_report(
            account_id=account_id,
            as_of=as_of,
            cost_method=cost_method,
            include_realtime=include_realtime,
        )

        png_bytes = generate_portfolio_share_image(
            snapshot=snapshot,
            risk_report=risk_data,
            show_pnl_amount=show_pnl_amount,
            show_pnl_pct=show_pnl_pct,
            show_equity=show_equity,
        )
        if not png_bytes:
            raise _internal_error("Share image generation unavailable (browser not found)", RuntimeError("no browser"))
        return Response(
            content=png_bytes,
            media_type="image/png",
            headers={"Content-Disposition": f'inline; filename="portfolio-share-{as_of or date.today().isoformat()}.png"'},
        )
    except ValueError as exc:
        raise _bad_request(exc)
    except HTTPException:
        raise
    except Exception as exc:
        raise _internal_error("Generate share image failed", exc)
