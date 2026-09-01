# -*- coding: utf-8 -*-
"""Portfolio service for P0 account/events/snapshot workflow."""

from __future__ import annotations

import json
import logging
import os
import time
from collections import defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import date, datetime, timedelta
from typing import Any, Dict, Iterable, List, Optional, Set, Tuple

from data_provider.base import canonical_stock_code, normalize_stock_code
from src.config import get_config
from src.repositories.portfolio_repo import (
    DuplicateTradeDedupHashError,
    DuplicateTradeUidError,
    PortfolioBusyError as RepoPortfolioBusyError,
    PortfolioRepository,
)

logger = logging.getLogger(__name__)

PortfolioBusyError = RepoPortfolioBusyError

try:
    import yfinance as yf
except Exception:  # pragma: no cover - optional dependency path
    yf = None

EPS = 1e-8


def _round_to_price_precision(value: float, price: float) -> float:
    """按现价小数位四舍五入（A股现价 2 位、ETF 3 位），用于账本当日盈亏口径。"""
    s = ("%f" % price).rstrip("0")
    if "." in s:
        return round(value, len(s.split(".")[1]))
    return round(value, 0)


def _has_new_trading_session_since(snap_date: date, until_date: date) -> bool:
    """判断自 snap_date 次日到 until_date 之间是否又存在交易日（出现过开盘时段）。"""
    from src.core.trading_calendar import is_market_open

    d = snap_date + timedelta(days=1)
    guard = 0
    while d <= until_date and guard < 15:
        if is_market_open("cn", d):
            return True
        d += timedelta(days=1)
        guard += 1
    return False


def _should_refresh_realtime_quotes(last_snap_created_at: Optional[datetime]) -> bool:
    """判断是否需要重新拉取实时行情（用户口径）。

    原则：若最近一次快照已是「收盘后」快照（覆盖当日收盘价），且自该快照后
    未再经历新的开盘时段，则行情不会发生变化，无需重新拉取，直接复用收盘快照。

    - 当前盘中（INTRADAY / CLOSING_AUCTION）→ 必须实时（价格持续变动）
    - 无法判断市场阶段（UNKNOWN）→ 保持实时行为（fail-open）
    - 无历史快照 → 拉一次建立数据
    - 上次快照是盘中 / 或跨过新开盘 → 需要实时
    - 上次快照是收盘后且期间未再开盘 → 不拉
    """
    from src.core.trading_calendar import infer_market_phase, MarketPhase

    from datetime import time as _clock_time

    phase = infer_market_phase("cn")
    if phase in (MarketPhase.INTRADAY, MarketPhase.CLOSING_AUCTION):
        return True
    if phase == MarketPhase.UNKNOWN:
        return True
    if last_snap_created_at is None:
        return True

    now = datetime.now()
    snap = last_snap_created_at
    if snap.tzinfo is not None:
        snap = snap.astimezone()
        snap = snap.replace(tzinfo=None)
    close_time = _clock_time(15, 0)  # 收盘整理时段阈值（收盘快照任务 15:10 后生成）
    is_close_snap = snap.time() >= close_time

    if snap.date() == now.date():
        # 同一天：盘中快照需要刷新到最新，收盘后快照直接复用
        return not is_close_snap

    # 跨天：今天尚未开盘（盘前）或今天非交易日时，行情仍是最近交易日收盘价，
    # 只有「收盘快照」且期间未再开盘才能复用；其余情况（今天已开盘过/午休/盘后）需刷新。
    if phase in (MarketPhase.PREMARKET, MarketPhase.NON_TRADING):
        if is_close_snap and not _has_new_trading_session_since(snap.date(), now.date()):
            return False
        return True
    # LUNCH_BREAK / POSTMARKET（今天已开盘过）→ 行情已变，需刷新
    return True


VALID_MARKETS = {"cn", "hk", "us", "jp", "kr", "tw"}
PARTIAL_VALUATION_MARKETS = {"jp", "kr", "tw"}
VALID_COST_METHODS = {"fifo", "avg"}
VALID_SIDES = {"buy", "sell"}
VALID_CASH_DIRECTIONS = {"in", "out"}
VALID_CORPORATE_ACTIONS = {"cash_dividend", "split_adjustment"}
PORTFOLIO_FX_REFRESH_DISABLED_REASON = "portfolio_fx_update_disabled"
PORTFOLIO_REALTIME_QUOTE_MAX_WORKERS = 4

# 实时行情短缓存：避免每次打开持仓页都实时抓取（东财/腾讯等接口慢且偶发失败），
# TTL 内重复请求直接复用上一次抓取结果，保证页面秒开。
# 注意：一次全量抓取本身可能耗时 30~50s，TTL 必须明显大于单次抓取耗时，
# 否则抓完时最早的条目已过期、缓存永远命不中。
# 第 4 个元素为拉取时刻是否处于盘中（INTRADAY / CLOSING_AUCTION）：
# 盘中拉取的价格不能跨阶段复用于非盘中（午休/收盘后必须重拉，拿到当日最新合法价）。
_REALTIME_PRICE_CACHE: Dict[str, Tuple[float, float, Optional[str], Optional[bool]]] = {}
_REALTIME_PRICE_CACHE_TTL_SECONDS = 120.0
# 同花顺账本实时价缓存（低频交叉校验）：30 分钟内复用，避免频繁请求账本接口。
_THS_LIVE_PRICE_CACHE: Dict[str, Tuple[float, Dict[str, float]]] = {}
_THS_LIVE_PRICE_TTL_SECONDS = 1800.0
# 实时行情当日涨跌幅缓存：与价格缓存同步更新，供 snapshot 的 day_change_pct 使用
_REALTIME_CHANGE_PCT_CACHE: Dict[str, Tuple[float, Optional[float]]] = {}


def _portfolio_limitations_for_market(market: str) -> List[str]:
    """Return explicit snapshot limitations for markets with partial valuation semantics."""

    if market not in PARTIAL_VALUATION_MARKETS:
        return []
    return [
        "realtime_quote_best_effort",
        "fx_and_cost_basis_partial",
        "sector_and_risk_metrics_limited",
    ]


def _merge_portfolio_limitations(*groups: Iterable[str]) -> List[str]:
    merged: List[str] = []
    seen: Set[str] = set()
    for group in groups:
        for item in group:
            if item and item not in seen:
                seen.add(item)
                merged.append(item)
    return merged


class PortfolioConflictError(Exception):
    """Raised when request conflicts with existing portfolio state."""


class PortfolioOversellError(ValueError):
    """Raised when a sell would exceed the available position quantity."""

    def __init__(
        self,
        *,
        symbol: str,
        trade_date: Optional[date],
        requested_quantity: float,
        available_quantity: float,
    ) -> None:
        self.symbol = symbol
        self.trade_date = trade_date
        self.requested_quantity = float(requested_quantity)
        self.available_quantity = max(0.0, float(available_quantity))
        date_hint = f" on {trade_date.isoformat()}" if trade_date is not None else ""
        super().__init__(
            "Oversell detected for "
            f"{symbol}{date_hint}: requested={round(self.requested_quantity, 8)}, "
            f"available={round(self.available_quantity, 8)}"
        )


@dataclass
class _AvgState:
    quantity: float = 0.0
    total_cost: float = 0.0


@dataclass(frozen=True)
class _ResolvedPositionPrice:
    price: float
    source: str
    price_date: Optional[date]
    is_stale: bool
    is_available: bool
    provider: Optional[str] = None


class PortfolioService:
    """Business logic for account CRUD, event writes, and snapshot replay."""

    def __init__(self, repo: Optional[PortfolioRepository] = None):
        self.repo = repo or PortfolioRepository()

    # ------------------------------------------------------------------
    # Account CRUD
    # ------------------------------------------------------------------
    def create_account(
        self,
        *,
        name: str,
        broker: Optional[str],
        market: str,
        base_currency: str,
        owner_id: Optional[str] = None,
    ) -> Dict[str, Any]:
        name_norm = (name or "").strip()
        if not name_norm:
            raise ValueError("name is required")
        market_norm = self._normalize_market(market)
        base_currency_norm = self._normalize_currency(base_currency)
        row = self.repo.create_account(
            name=name_norm,
            broker=(broker or "").strip() or None,
            market=market_norm,
            base_currency=base_currency_norm,
            owner_id=(owner_id or "").strip() or None,
        )
        return self._account_to_dict(row)

    def list_accounts(self, include_inactive: bool = False) -> List[Dict[str, Any]]:
        rows = self.repo.list_accounts(include_inactive=include_inactive)
        return [self._account_to_dict(r) for r in rows]

    def update_account(
        self,
        account_id: int,
        *,
        name: Optional[str] = None,
        broker: Optional[str] = None,
        market: Optional[str] = None,
        base_currency: Optional[str] = None,
        owner_id: Optional[str] = None,
        is_active: Optional[bool] = None,
    ) -> Optional[Dict[str, Any]]:
        fields: Dict[str, Any] = {}
        if name is not None:
            name_norm = name.strip()
            if not name_norm:
                raise ValueError("name is required")
            fields["name"] = name_norm
        if broker is not None:
            fields["broker"] = broker.strip() or None
        if market is not None:
            fields["market"] = self._normalize_market(market)
        if base_currency is not None:
            fields["base_currency"] = self._normalize_currency(base_currency)
        if owner_id is not None:
            fields["owner_id"] = owner_id.strip() or None
        if is_active is not None:
            fields["is_active"] = bool(is_active)
        if not fields:
            raise ValueError("No fields provided for update")

        row = self.repo.update_account(account_id, fields)
        if row is None:
            return None
        return self._account_to_dict(row)

    def deactivate_account(self, account_id: int) -> bool:
        return self.repo.deactivate_account(account_id)

    # ------------------------------------------------------------------
    # Event writes
    # ------------------------------------------------------------------
    def record_trade(
        self,
        *,
        account_id: int,
        symbol: str,
        trade_date: date,
        side: str,
        quantity: float,
        price: float,
        fee: float = 0.0,
        tax: float = 0.0,
        market: Optional[str] = None,
        currency: Optional[str] = None,
        trade_uid: Optional[str] = None,
        dedup_hash: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        side_norm = (side or "").strip().lower()
        if side_norm not in VALID_SIDES:
            raise ValueError("side must be buy or sell")
        if quantity <= 0 or price <= 0:
            raise ValueError("quantity and price must be > 0")
        if fee < 0 or tax < 0:
            raise ValueError("fee and tax must be >= 0")
        symbol_norm = self._normalize_symbol_for_storage(symbol)
        if not symbol_norm:
            raise ValueError("symbol is required")
        trade_uid_norm = (trade_uid or "").strip() or None
        dedup_hash_norm = (dedup_hash or "").strip() or None
        try:
            with self.repo.portfolio_write_session() as session:
                account = self._require_active_account_in_session(session=session, account_id=account_id)
                market_norm = self._normalize_market(market or account.market)
                currency_norm = self._normalize_currency(currency or self._default_currency_for_market(market_norm))
                self._validate_trade_identity(
                    account_id=account_id,
                    trade_uid=trade_uid_norm,
                    dedup_hash=dedup_hash_norm,
                    session=session,
                    )
                if side_norm == "sell":
                    self._validate_sell_quantity(
                        account_id=account_id,
                        symbol=symbol,
                        market=market_norm,
                        currency=currency_norm,
                        trade_date=trade_date,
                        quantity=float(quantity),
                        session=session,
                    )
                row = self.repo.add_trade_in_session(
                    session=session,
                    account_id=account_id,
                    trade_uid=trade_uid_norm,
                    symbol=symbol_norm,
                    market=market_norm,
                    currency=currency_norm,
                    trade_date=trade_date,
                    side=side_norm,
                    quantity=float(quantity),
                    price=float(price),
                    fee=float(fee),
                    tax=float(tax),
                    note=(note or "").strip() or None,
                    dedup_hash=dedup_hash_norm,
                )
                return {"id": int(row.id)}
        except (DuplicateTradeUidError, DuplicateTradeDedupHashError) as exc:
            raise PortfolioConflictError(str(exc)) from exc

    def record_cash_ledger(
        self,
        *,
        account_id: int,
        event_date: date,
        direction: str,
        amount: float,
        currency: Optional[str] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        direction_norm = (direction or "").strip().lower()
        if direction_norm not in VALID_CASH_DIRECTIONS:
            raise ValueError("direction must be in or out")
        if amount <= 0:
            raise ValueError("amount must be > 0")
        with self.repo.portfolio_write_session() as session:
            account = self._require_active_account_in_session(session=session, account_id=account_id)
            currency_norm = self._normalize_currency(currency or account.base_currency)
            row = self.repo.add_cash_ledger_in_session(
                session=session,
                account_id=account_id,
                event_date=event_date,
                direction=direction_norm,
                amount=float(amount),
                currency=currency_norm,
                note=(note or "").strip() or None,
            )
            return {"id": int(row.id)}

    def record_corporate_action(
        self,
        *,
        account_id: int,
        symbol: str,
        effective_date: date,
        action_type: str,
        market: Optional[str] = None,
        currency: Optional[str] = None,
        cash_dividend_per_share: Optional[float] = None,
        split_ratio: Optional[float] = None,
        note: Optional[str] = None,
    ) -> Dict[str, Any]:
        action_type_norm = (action_type or "").strip().lower()
        if action_type_norm not in VALID_CORPORATE_ACTIONS:
            raise ValueError("action_type must be cash_dividend or split_adjustment")

        if action_type_norm == "cash_dividend":
            if cash_dividend_per_share is None or cash_dividend_per_share < 0:
                raise ValueError("cash_dividend_per_share must be >= 0 for cash_dividend")
        if action_type_norm == "split_adjustment":
            if split_ratio is None or split_ratio <= 0:
                raise ValueError("split_ratio must be > 0 for split_adjustment")
        with self.repo.portfolio_write_session() as session:
            account = self._require_active_account_in_session(session=session, account_id=account_id)
            market_norm = self._normalize_market(market or account.market)
            currency_norm = self._normalize_currency(currency or self._default_currency_for_market(market_norm))
            symbol_norm = self._normalize_symbol_for_storage(symbol)
            if not symbol_norm:
                raise ValueError("symbol is required")
            row = self.repo.add_corporate_action_in_session(
                session=session,
                account_id=account_id,
                symbol=symbol_norm,
                market=market_norm,
                currency=currency_norm,
                effective_date=effective_date,
                action_type=action_type_norm,
                cash_dividend_per_share=cash_dividend_per_share,
                split_ratio=split_ratio,
                note=(note or "").strip() or None,
            )
            return {"id": int(row.id)}

    def delete_trade_event(self, trade_id: int) -> bool:
        with self.repo.portfolio_write_session() as session:
            return self.repo.delete_trade_in_session(session=session, trade_id=trade_id)

    def delete_cash_ledger_event(self, entry_id: int) -> bool:
        with self.repo.portfolio_write_session() as session:
            return self.repo.delete_cash_ledger_in_session(session=session, entry_id=entry_id)

    def delete_corporate_action_event(self, action_id: int) -> bool:
        with self.repo.portfolio_write_session() as session:
            return self.repo.delete_corporate_action_in_session(session=session, action_id=action_id)

    def list_trade_events(
        self,
        *,
        account_id: Optional[int] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        symbol: Optional[str] = None,
        side: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        if account_id is not None:
            self._require_active_account(account_id)
        page, page_size = self._validate_paging(page=page, page_size=page_size)
        if date_from is not None and date_to is not None and date_from > date_to:
            raise ValueError("date_from must be <= date_to")

        symbol_filters: Optional[List[str]] = None
        if symbol is not None and symbol.strip():
            symbol_filters = self._build_symbol_filter_values(symbol)
            if not symbol_filters:
                raise ValueError("symbol is invalid")

        side_norm: Optional[str] = None
        if side is not None and side.strip():
            side_norm = side.strip().lower()
            if side_norm not in VALID_SIDES:
                raise ValueError("side must be buy or sell")

        rows, total = self.repo.query_trades(
            account_id=account_id,
            date_from=date_from,
            date_to=date_to,
            symbols=symbol_filters,
            side=side_norm,
            page=page,
            page_size=page_size,
        )
        return {
            "items": [self._trade_row_to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def list_cash_ledger_events(
        self,
        *,
        account_id: Optional[int] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        direction: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        if account_id is not None:
            self._require_active_account(account_id)
        page, page_size = self._validate_paging(page=page, page_size=page_size)
        if date_from is not None and date_to is not None and date_from > date_to:
            raise ValueError("date_from must be <= date_to")

        direction_norm: Optional[str] = None
        if direction is not None and direction.strip():
            direction_norm = direction.strip().lower()
            if direction_norm not in VALID_CASH_DIRECTIONS:
                raise ValueError("direction must be in or out")

        rows, total = self.repo.query_cash_ledger(
            account_id=account_id,
            date_from=date_from,
            date_to=date_to,
            direction=direction_norm,
            page=page,
            page_size=page_size,
        )
        return {
            "items": [self._cash_ledger_row_to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    def list_corporate_action_events(
        self,
        *,
        account_id: Optional[int] = None,
        date_from: Optional[date] = None,
        date_to: Optional[date] = None,
        symbol: Optional[str] = None,
        action_type: Optional[str] = None,
        page: int = 1,
        page_size: int = 20,
    ) -> Dict[str, Any]:
        if account_id is not None:
            self._require_active_account(account_id)
        page, page_size = self._validate_paging(page=page, page_size=page_size)
        if date_from is not None and date_to is not None and date_from > date_to:
            raise ValueError("date_from must be <= date_to")

        symbol_filters: Optional[List[str]] = None
        if symbol is not None and symbol.strip():
            symbol_filters = self._build_symbol_filter_values(symbol)
            if not symbol_filters:
                raise ValueError("symbol is invalid")

        action_norm: Optional[str] = None
        if action_type is not None and action_type.strip():
            action_norm = action_type.strip().lower()
            if action_norm not in VALID_CORPORATE_ACTIONS:
                raise ValueError("action_type must be cash_dividend or split_adjustment")

        rows, total = self.repo.query_corporate_actions(
            account_id=account_id,
            date_from=date_from,
            date_to=date_to,
            symbols=symbol_filters,
            action_type=action_norm,
            page=page,
            page_size=page_size,
        )
        return {
            "items": [self._corporate_action_row_to_dict(row) for row in rows],
            "total": total,
            "page": page,
            "page_size": page_size,
        }

    # ------------------------------------------------------------------
    # Snapshot replay
    # ------------------------------------------------------------------
    def get_portfolio_snapshot(
        self,
        *,
        account_id: Optional[int] = None,
        as_of: Optional[date] = None,
        cost_method: str = "fifo",
        include_realtime: bool = True,
    ) -> Dict[str, Any]:
        as_of_date = as_of or date.today()
        method = self._normalize_cost_method(cost_method)

        if account_id is not None:
            account = self._require_active_account(account_id)
            account_rows = [account]
        else:
            account_rows = self.repo.list_accounts(include_inactive=False)

        accounts_payload: List[Dict[str, Any]] = []
        aggregate_currency = "CNY"
        aggregate = {
            "total_cash": 0.0,
            "total_market_value": 0.0,
            "total_equity": 0.0,
            "realized_pnl": 0.0,
            "unrealized_pnl": 0.0,
            "fee_total": 0.0,
            "tax_total": 0.0,
            "fx_stale": False,
            "limitations": [],
        }
        # 提前拉取同花顺账本持仓（持有盈亏/最新价/名称），供盘后不拉行情时覆盖价格与持有盈亏
        if not hasattr(self, "_ths_hold_pnl_cache"):
            self._ths_hold_pnl_cache = self._fetch_ths_hold_pnl_map()
        ths_hold_map = self._ths_hold_pnl_cache
        ths_price_map = (
            {code: info["price"] for code, info in ths_hold_map.items() if info.get("price")}
            if ths_hold_map
            else None
        )
        ths_name_map = (
            {code: info["name"] for code, info in ths_hold_map.items() if info.get("name")}
            if ths_hold_map
            else None
        )

        for account in account_rows:
            # 盘后/非交易日且上次快照已是收盘价时，不再拉实时行情（复用收盘快照，加速启动与页面加载）
            effective_include_realtime = include_realtime
            snapshot_change_pct_map = None
            effective_ths_price_map = None
            if effective_include_realtime:
                effective_include_realtime = _should_refresh_realtime_quotes(
                    self._latest_snapshot_created_at(account.id, as_of_date, method)
                )
                if not effective_include_realtime:
                    snapshot_change_pct_map = self._load_latest_snapshot_change_pct_map(
                        account.id, as_of_date, method
                    )
                    # 盘后用账本最新价（今日收盘价）作为本地价格，保持市值/总资产为当日值
                    effective_ths_price_map = ths_price_map
            else:
                # 显式 include_realtime=False（如对账）时，同样用账本最新价作为本地价格，
                # 保证本地市值与网页账本、持仓页一致（账本价：盘前=昨收、盘中=实时、盘后=今收）
                effective_ths_price_map = ths_price_map
            account_snapshot = self._replay_account(
                account=account,
                as_of_date=as_of_date,
                cost_method=method,
                include_realtime=effective_include_realtime,
                snapshot_change_pct_map=snapshot_change_pct_map,
                ths_price_map=effective_ths_price_map,
                ths_name_map=ths_name_map,
            )

            # 若账户有持仓但市值缺失（价格获取失败，常见于非交易日），
            # 不覆盖已有快照，避免把总资产写成接近 0 造成曲线断崖。
            has_positions = bool(account_snapshot["positions_cache"])
            mv_ok = float(account_snapshot["total_market_value"]) > 0
            if has_positions and not mv_ok:
                accounts_payload.append(account_snapshot["public"])
                aggregate["limitations"] = _merge_portfolio_limitations(
                    aggregate["limitations"],
                    account_snapshot["public"].get("limitations", []),
                )
                continue

            self.repo.replace_positions_lots_and_snapshot(
                account_id=account.id,
                snapshot_date=as_of_date,
                cost_method=method,
                base_currency=account.base_currency,
                total_cash=account_snapshot["total_cash"],
                total_market_value=account_snapshot["total_market_value"],
                total_equity=account_snapshot["total_equity"],
                unrealized_pnl=account_snapshot["unrealized_pnl"],
                realized_pnl=account_snapshot["realized_pnl"],
                fee_total=account_snapshot["fee_total"],
                tax_total=account_snapshot["tax_total"],
                fx_stale=account_snapshot["fx_stale"],
                payload=json.dumps(account_snapshot["payload"], ensure_ascii=False),
                positions=account_snapshot["positions_cache"],
                lots=account_snapshot["lots_cache"],
                valuation_currency=account.base_currency,
            )

            accounts_payload.append(account_snapshot["public"])
            aggregate["limitations"] = _merge_portfolio_limitations(
                aggregate["limitations"],
                account_snapshot["public"].get("limitations", []),
            )

            cash_cny, stale_cash, _ = self._convert_amount(
                amount=account_snapshot["total_cash"],
                from_currency=account.base_currency,
                to_currency=aggregate_currency,
                as_of_date=as_of_date,
            )
            mv_cny, stale_mv, _ = self._convert_amount(
                amount=account_snapshot["total_market_value"],
                from_currency=account.base_currency,
                to_currency=aggregate_currency,
                as_of_date=as_of_date,
            )
            eq_cny, stale_eq, _ = self._convert_amount(
                amount=account_snapshot["total_equity"],
                from_currency=account.base_currency,
                to_currency=aggregate_currency,
                as_of_date=as_of_date,
            )
            realized_cny, stale_realized, _ = self._convert_amount(
                amount=account_snapshot["realized_pnl"],
                from_currency=account.base_currency,
                to_currency=aggregate_currency,
                as_of_date=as_of_date,
            )
            unrealized_cny, stale_unrealized, _ = self._convert_amount(
                amount=account_snapshot["unrealized_pnl"],
                from_currency=account.base_currency,
                to_currency=aggregate_currency,
                as_of_date=as_of_date,
            )
            fee_cny, stale_fee, _ = self._convert_amount(
                amount=account_snapshot["fee_total"],
                from_currency=account.base_currency,
                to_currency=aggregate_currency,
                as_of_date=as_of_date,
            )
            tax_cny, stale_tax, _ = self._convert_amount(
                amount=account_snapshot["tax_total"],
                from_currency=account.base_currency,
                to_currency=aggregate_currency,
                as_of_date=as_of_date,
            )

            aggregate["total_cash"] += cash_cny
            aggregate["total_market_value"] += mv_cny
            aggregate["total_equity"] += eq_cny
            aggregate["realized_pnl"] += realized_cny
            aggregate["unrealized_pnl"] += unrealized_cny
            aggregate["fee_total"] += fee_cny
            aggregate["tax_total"] += tax_cny
            aggregate["fx_stale"] = aggregate["fx_stale"] or any(
                [
                    stale_cash,
                    stale_mv,
                    stale_eq,
                    stale_realized,
                    stale_unrealized,
                    stale_fee,
                    stale_tax,
                ]
            )

        # 汇总口径：今日盈亏/持有盈亏比例/指数行情（账户均以 CNY 汇总时准确，
        # 单一币种账户为最常见场景；多币种账户比例按汇总值近似）。
        # 今日盈亏：本地逐票(现价-昨收)计算（盘后现价取账本今日收盘价，昨收取本地行情，
        # 与账本网页「当日盈亏」口径一致）；持有盈亏优先取账本口径（含分红/费用调整），
        # 账本不可用时回退本地（市值-成本）。
        # 今日盈亏：盘后/非交易日优先用账本导出快照（最新交易日），盘中用腾讯实时行情
        agg_day_pnl, agg_prev_mv = self._resolve_day_pnl(
            [p for acc in accounts_payload for p in acc.get("positions", [])],
            float(aggregate["total_market_value"]),
        )
        agg_day_pnl_pct = (agg_day_pnl / agg_prev_mv * 100.0) if agg_prev_mv > 0 else None
        agg_total_cost = sum(
            float(pos.get("total_cost") or 0.0)
            for acc in accounts_payload
            for pos in acc.get("positions", [])
        )
        agg_unrealized_pct = (
            (aggregate["unrealized_pnl"] / agg_total_cost * 100.0)
            if abs(agg_total_cost) > EPS
            else None
        )
        # 持有盈亏优先用同花顺账本口径（含分红/费用调整，与手机端口径一致）；
        # 账本不可用时回退本地（市值-成本）。
        agg_hold_pnl = aggregate["unrealized_pnl"]
        agg_hold_pnl_pct = agg_unrealized_pct
        if ths_hold_map:
            matched_codes: Set[str] = set()
            matched_hold_pnl = 0.0
            matched_cost = 0.0
            for acc in accounts_payload:
                for pos in acc.get("positions", []):
                    code = str(pos.get("symbol") or "").strip()
                    if code not in ths_hold_map or code in matched_codes:
                        continue
                    matched_codes.add(code)
                    info = ths_hold_map[code]
                    matched_hold_pnl += float(info.get("hold_pnl") or 0.0)
                    matched_cost += float(info.get("cost") or 0.0)
            if matched_codes:
                agg_hold_pnl = matched_hold_pnl
                agg_hold_pnl_pct = (matched_hold_pnl / matched_cost * 100.0) if abs(matched_cost) > EPS else None
        try:
            market_indices = self._fetch_market_indices()
        except Exception:  # noqa: BLE001 - 指数是增强项，失败返回空
            market_indices = []

        return {
            "as_of": as_of_date.isoformat(),
            "cost_method": method,
            "currency": aggregate_currency,
            "account_count": len(account_rows),
            "total_cash": round(aggregate["total_cash"], 6),
            "total_market_value": round(aggregate["total_market_value"], 6),
            "total_equity": round(aggregate["total_equity"], 6),
            "realized_pnl": round(aggregate["realized_pnl"], 6),
            "unrealized_pnl": round(aggregate["unrealized_pnl"], 6),
            "unrealized_pnl_pct": round(agg_unrealized_pct, 6) if agg_unrealized_pct is not None else None,
            "day_pnl": round(agg_day_pnl, 6),
            "day_pnl_pct": round(agg_day_pnl_pct, 6) if agg_day_pnl_pct is not None else None,
            "hold_pnl": round(agg_hold_pnl, 6),
            "hold_pnl_pct": round(agg_hold_pnl_pct, 6) if agg_hold_pnl_pct is not None else None,
            "fee_total": round(aggregate["fee_total"], 6),
            "tax_total": round(aggregate["tax_total"], 6),
            "fx_stale": aggregate["fx_stale"],
            "data_quality": "partial" if aggregate["limitations"] else "ok",
            "limitations": aggregate["limitations"],
            "indices": market_indices,
            "accounts": accounts_payload,
        }

    def _resolve_day_pnl(
        self, positions: List[Dict[str, Any]], total_market_value: float
    ) -> Tuple[float, float]:
        """今日盈亏（与账本网页口径一致）：

        盘后/非交易日：若存在最新交易日（跳过周末）的账本导出快照，优先采用其当日盈亏；
        否则（盘中，或快照缺失/过期）用腾讯实时行情 (现价-昨收)*数量 计算。
        返回 (今日盈亏, 昨收市值)。
        """
        now = datetime.now()
        in_trading = (
            now.weekday() < 5
            and 9 * 60 + 30 <= now.hour * 60 + now.minute <= 15 * 60
        )
        if not in_trading:
            snap = self._read_export_snapshot()
            if snap and snap.get("as_of") == self._last_trading_day(now.date()).isoformat():
                day_pnl = snap.get("day_pnl")
                if day_pnl is not None:
                    day_pnl = float(day_pnl)
                    prev = (snap.get("total_market_value") or total_market_value) - day_pnl
                    if prev > 0:
                        return day_pnl, prev
        # 腾讯实时行情口径（与分享卡片/账本网页一致）
        try:
            from src.portfolio_share import _fetch_tencent_realtime_quotes

            quotes = _fetch_tencent_realtime_quotes(
                [str(p.get("symbol")) for p in positions if float(p.get("quantity") or 0) > 0]
            )
        except Exception:  # noqa: BLE001
            quotes = {}
        total_pnl = 0.0
        total_prev = 0.0
        for p in positions:
            qty = float(p.get("quantity") or 0.0)
            if qty <= 0:
                continue
            q = quotes.get(str(p.get("symbol")))
            if q and q.get("prev_close") and q.get("current"):
                prev = float(q["prev_close"])
                cur = float(q["current"])
                if prev > 0 and cur > 0:
                    total_pnl += (cur - prev) * qty
                    total_prev += prev * qty
        if total_prev <= 0:
            return 0.0, total_market_value
        return total_pnl, total_prev

    @staticmethod
    def _last_trading_day(d: date) -> date:
        """跳过周末的最近交易日（节假日未内置，按工作日近似）。"""
        while d.weekday() >= 5:
            d -= timedelta(days=1)
        return d

    @staticmethod
    def _read_export_snapshot() -> Optional[Dict[str, Any]]:
        """读取账本导出快照（ths_export_snapshot.json），无则返回 None。"""
        try:
            from src.services.ths_sync.ths_sync_service import ThsSyncService

            svc = ThsSyncService()
            path = os.path.join(os.path.dirname(svc.cookie_file), "ths_export_snapshot.json")
            if not os.path.exists(path):
                return None
            with open(path, "r", encoding="utf-8") as fh:
                return json.load(fh)
        except Exception:  # noqa: BLE001
            return None

    @staticmethod
    def _fetch_ths_hold_pnl_map() -> Optional[Dict[str, Dict[str, float]]]:
        """best-effort 拉取同花顺账本「汇总持仓」的持有盈亏与最新价（含分红/费用调整）。

        只拉各账户持仓接口（不拉交易流水），跨账户按代码合并；
        返回 {code: {"hold_pnl","cost","price","value"}}；
        账本未登录/调用失败时返回 None（调用方回退本地计算）。

        注意：账本接口的 pre_profit/close_profit 均非网页「当日盈亏」口径
        （网页当日盈亏 = 行情(现价-昨收)×数量），故不在此返回，当日盈亏由本地行情计算。
        """
        try:
            from src.services.ths_sync.ths_sync_service import ThsSyncService

            svc = ThsSyncService()
            if not svc.client.is_logged_in():
                return None
            merged: Dict[str, Dict[str, Any]] = {}
            for acc in svc.client.fetch_accounts():
                if acc["type"] != "common":
                    continue
                try:
                    pos = svc.client.fetch_positions(acc["fund_key"])
                except Exception:  # noqa: BLE001 - 单账户失败跳过
                    continue
                for p in pos.get("positions", []):
                    code = str(p.get("code") or "").strip()
                    if not code:
                        continue
                    hold_pnl = float(p.get("hold_profit") or 0.0)
                    value = float(p.get("value") or 0.0)
                    cost = value - hold_pnl
                    price = float(p.get("price") or 0.0)
                    name = str(p.get("name") or "").strip() or None
                    if code in merged:
                        m = merged[code]
                        m["hold_pnl"] += hold_pnl
                        m["cost"] += cost
                        m["value"] += value
                    else:
                        merged[code] = {
                            "hold_pnl": hold_pnl,
                            "cost": cost,
                            "price": price,
                            "value": value,
                            "name": name,
                        }
            if not merged:
                return None
            out: Dict[str, Dict[str, Any]] = {}
            for code, m in merged.items():
                out[code] = {
                    "hold_pnl": round(float(m["hold_pnl"]), 6),
                    "cost": round(float(m["cost"]), 6),
                    "price": round(float(m["price"]), 6),
                    "value": round(float(m["value"]), 6),
                    "name": m.get("name"),
                }
            return out
        except Exception as exc:  # noqa: BLE001 - 增强项，失败静默
            logger.warning("Failed to fetch THS hold pnl: %s", exc)
            return None

    def _latest_snapshot_created_at(
        self,
        account_id: int,
        as_of_date: date,
        cost_method: str,
    ) -> Optional[datetime]:
        """返回账户最近一条持仓快照的生成时间（用于判断是否需重拉实时行情）。"""
        try:
            rows = self.repo.list_daily_snapshots_for_risk(
                as_of=as_of_date,
                cost_method=cost_method,
                account_id=account_id,
                lookback_days=62,
            )
            if not rows:
                return None
            return rows[-1].created_at
        except Exception as exc:  # noqa: BLE001 - 查询失败时按需实时（fail-open）
            logger.warning("latest snapshot query failed: %s", exc)
            return None

    def _load_latest_snapshot_change_pct_map(
        self,
        account_id: int,
        as_of_date: date,
        cost_method: str,
    ) -> Optional[Dict[str, Optional[float]]]:
        """从最近一条快照 payload 读回各持仓的当日涨跌幅（盘后复用收盘快照时保留今日盈亏）。"""
        try:
            rows = self.repo.list_daily_snapshots_for_risk(
                as_of=as_of_date,
                cost_method=cost_method,
                account_id=account_id,
                lookback_days=62,
            )
            if not rows:
                return None
            payload = json.loads(rows[-1].payload) if rows[-1].payload else {}
            out: Dict[str, Optional[float]] = {}
            for pos in payload.get("positions") or []:
                symbol = str(pos.get("symbol") or "").strip()
                if not symbol:
                    continue
                dcp = pos.get("day_change_pct")
                out[symbol] = float(dcp) if dcp is not None else None
            return out
        except Exception as exc:  # noqa: BLE001 - 读取失败静默（仅影响今日盈亏展示）
            logger.warning("snapshot change pct load failed: %s", exc)
            return None

    @staticmethod
    def _fetch_market_indices() -> List[Dict[str, Any]]:
        """拉取上证指数 / 深证成指实时点位与涨跌幅（腾讯行情，best-effort）。"""
        try:
            import urllib.request

            url = "https://qt.gtimg.cn/q=sh000001,sz399001"
            req = urllib.request.Request(
                url,
                headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
            )
            data = urllib.request.urlopen(req, timeout=8).read().decode("gbk", "ignore")
            out: List[Dict[str, Any]] = []
            for line in data.strip().split(";"):
                line = line.strip()
                if "=" not in line:
                    continue
                _, val = line.split("=", 1)
                fields = val.strip('"').split("~")
                if len(fields) < 40:
                    continue
                try:
                    value = float(fields[3] or 0)
                    change_pct = float(fields[32] or 0)
                except (TypeError, ValueError):
                    continue
                out.append({
                    "code": fields[2],
                    "name": fields[1],
                    "value": value,
                    "change_pct": change_pct,
                })
            return out
        except Exception as exc:  # noqa: BLE001
            logger.warning("Failed to fetch market indices: %s", exc)
            return []

    def refresh_fx_rates(
        self,
        *,
        account_id: Optional[int] = None,
        as_of: Optional[date] = None,
    ) -> Dict[str, Any]:
        """Refresh account FX pairs online with stale fallback when fetch fails."""
        as_of_date = as_of or date.today()
        config = get_config()
        refresh_enabled = bool(getattr(config, "portfolio_fx_update_enabled", True))
        if account_id is not None:
            account_rows = [self._require_active_account(account_id)]
        else:
            account_rows = self.repo.list_accounts(include_inactive=False)

        summary = {
            "as_of": as_of_date.isoformat(),
            "account_count": len(account_rows),
            "refresh_enabled": refresh_enabled,
            "disabled_reason": None if refresh_enabled else PORTFOLIO_FX_REFRESH_DISABLED_REASON,
            "pair_count": 0,
            "updated_count": 0,
            "stale_count": 0,
            "error_count": 0,
        }
        for account in account_rows:
            item = self._refresh_account_fx_rates(
                account=account,
                as_of_date=as_of_date,
                refresh_enabled=refresh_enabled,
            )
            summary["pair_count"] += item["pair_count"]
            summary["updated_count"] += item["updated_count"]
            summary["stale_count"] += item["stale_count"]
            summary["error_count"] += item["error_count"]
        return summary

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------
    def _validate_trade_identity(
        self,
        *,
        account_id: int,
        trade_uid: Optional[str],
        dedup_hash: Optional[str],
        session: Optional[Any] = None,
    ) -> None:
        if trade_uid and self._has_trade_uid(account_id=account_id, trade_uid=trade_uid, session=session):
            raise PortfolioConflictError(f"Duplicate trade_uid for account_id={account_id}: {trade_uid}")
        if dedup_hash and self._has_trade_dedup_hash(account_id=account_id, dedup_hash=dedup_hash, session=session):
            raise PortfolioConflictError(f"Duplicate dedup_hash for account_id={account_id}: {dedup_hash}")

    def _validate_sell_quantity(
        self,
        *,
        account_id: int,
        symbol: str,
        market: str,
        currency: str,
        trade_date: date,
        quantity: float,
        session: Optional[Any] = None,
    ) -> None:
        key = (
            self._normalize_symbol_for_position(symbol),
            self._normalize_market(market),
            self._normalize_currency(currency),
        )
        available_quantity = self._calculate_available_quantity(
            account_id=account_id,
            key=key,
            as_of_date=trade_date,
            session=session,
        )
        if available_quantity + EPS < quantity:
            raise PortfolioOversellError(
                symbol=key[0],
                trade_date=trade_date,
                requested_quantity=quantity,
                available_quantity=available_quantity,
            )

    def _calculate_available_quantity(
        self,
        *,
        account_id: int,
        key: Tuple[str, str, str],
        as_of_date: date,
        session: Optional[Any] = None,
    ) -> float:
        if session is None:
            trades = self.repo.list_trades(account_id, as_of=as_of_date)
            corporate_actions = self.repo.list_corporate_actions(account_id, as_of=as_of_date)
        else:
            trades = self.repo.list_trades_in_session(session=session, account_id=account_id, as_of=as_of_date)
            corporate_actions = self.repo.list_corporate_actions_in_session(
                session=session,
                account_id=account_id,
                as_of=as_of_date,
            )

        events = []
        for row in corporate_actions:
            event_key = (
                self._normalize_symbol_for_position(row.symbol),
                self._normalize_market(row.market),
                self._normalize_currency(row.currency),
            )
            if event_key == key:
                events.append(("corp", row.effective_date, row.id, row))
        for row in trades:
            event_key = (
                self._normalize_symbol_for_position(row.symbol),
                self._normalize_market(row.market),
                self._normalize_currency(row.currency),
            )
            if event_key == key:
                events.append(("trade", row.trade_date, row.id, row))

        # Quantity validation only depends on position-changing events for one symbol.
        # Cash ledger entries do not affect shares held, so we keep the same corp->trade
        # ordering as full replay without pulling unrelated cash events into this path.
        event_priority = {"corp": 1, "trade": 2}
        events.sort(key=lambda item: (item[1], event_priority[item[0]], item[2]))

        quantity_held = 0.0
        for event_type, event_date, _, event in events:
            if event_type == "corp":
                action_type = (event.action_type or "").strip().lower()
                if action_type != "split_adjustment":
                    continue
                split_ratio = float(event.split_ratio or 0.0)
                if split_ratio <= 0:
                    raise ValueError(f"Invalid split_ratio for {key[0]}")
                if abs(split_ratio - 1.0) <= EPS:
                    continue
                quantity_held *= split_ratio
                continue

            qty = float(event.quantity or 0.0)
            if qty <= 0:
                raise ValueError(f"Invalid trade quantity for {key[0]}")
            side = (event.side or "").strip().lower()
            if side == "buy":
                quantity_held += qty
                continue
            if side != "sell":
                raise ValueError(f"Unsupported trade side: {event.side}")
            if quantity_held + EPS < qty:
                raise PortfolioOversellError(
                    symbol=key[0],
                    trade_date=event_date,
                    requested_quantity=qty,
                    available_quantity=quantity_held,
                )
            quantity_held -= qty
            if quantity_held <= EPS:
                quantity_held = 0.0

        return quantity_held

    def build_monthly_statement(
        self,
        *,
        month: str,
        account_id: Optional[int] = None,
        cost_method: str = "fifo",
    ) -> Dict[str, Any]:
        """月度对账单：聚合当月交易/资金/分红，并用日快照计算期初期末资产与月收益率。"""
        try:
            y, m = (int(p) for p in str(month).split("-"))
        except Exception:
            raise ValueError("month must be YYYY-MM")
        if m < 1 or m > 12:
            raise ValueError("month must be YYYY-MM")
        date_from = date(y, m, 1)
        next_month = date(y + 1, 1, 1) if m == 12 else date(y, m + 1, 1)
        date_to = next_month - timedelta(days=1)
        method = self._normalize_cost_method(cost_method)

        trades, _ = self.repo.query_trades(
            account_id=account_id,
            date_from=date_from,
            date_to=date_to,
            symbols=None,
            side=None,
            page=1,
            page_size=10000,
        )
        cash_rows, _ = self.repo.query_cash_ledger(
            account_id=account_id,
            date_from=date_from,
            date_to=date_to,
            direction=None,
            page=1,
            page_size=10000,
        )
        ca_rows, _ = self.repo.query_corporate_actions(
            account_id=account_id,
            date_from=date_from,
            date_to=date_to,
            symbols=None,
            action_type=None,
            page=1,
            page_size=10000,
        )

        buy_count = 0
        buy_amount = 0.0
        buy_fee = 0.0
        sell_count = 0
        sell_amount = 0.0
        sell_fee = 0.0
        details: List[Dict[str, Any]] = []
        for t in trades:
            qty = float(t.quantity or 0)
            px = float(t.price or 0)
            amt = qty * px
            fee = float(t.fee or 0) + float(t.tax or 0)
            side = (t.side or "").strip().lower()
            # 过滤数量为 0 的无效记录（账本同步可能带入空交易噪音）
            if qty <= 0:
                continue
            details.append({
                "date": t.trade_date.isoformat(),
                "symbol": t.symbol,
                "side": "buy" if side == "buy" else "sell",
                "quantity": qty,
                "price": px,
                "amount": round(amt, 2),
                "fee": round(fee, 2),
            })
            if side == "buy":
                buy_count += 1
                buy_amount += amt
                buy_fee += fee
            else:
                sell_count += 1
                sell_amount += amt
                sell_fee += fee

        inflow = 0.0
        outflow = 0.0
        for c in cash_rows:
            if (c.direction or "").strip().lower() == "in":
                inflow += float(c.amount or 0)
            else:
                outflow += float(c.amount or 0)

        dividends: List[Dict[str, Any]] = []
        for ca in ca_rows:
            if (ca.action_type or "").strip().lower() == "cash_dividend":
                dividends.append({
                    "date": ca.effective_date.isoformat(),
                    "symbol": ca.symbol,
                    "per_share": float(ca.cash_dividend_per_share or 0),
                })

        # 期初/期末资产（日快照）：期末取月末最近一天，期初取月初之前最近一天
        snapshots = self.repo.list_daily_snapshots_for_risk(
            as_of=date_to,
            cost_method=method,
            account_id=account_id,
            lookback_days=62,
        )
        end_snap = snapshots[-1] if snapshots else None
        begin_snap: Any = None
        for s in snapshots:
            if s.snapshot_date < date_from:
                begin_snap = s

        def _eq(snap: Any) -> Optional[float]:
            return float(snap.total_equity or 0) if snap is not None else None

        begin_equity = _eq(begin_snap)
        end_equity = _eq(end_snap)
        ret_pct = None
        if begin_equity and begin_equity != 0 and end_equity is not None:
            ret_pct = round((end_equity - begin_equity) / begin_equity * 100, 2)

        return {
            "month": month,
            "account_id": account_id,
            "trades": {
                "buy_count": buy_count,
                "buy_amount": round(buy_amount, 2),
                "buy_fee": round(buy_fee, 2),
                "sell_count": sell_count,
                "sell_amount": round(sell_amount, 2),
                "sell_fee": round(sell_fee, 2),
                "net_cash_outflow": round(buy_amount + buy_fee - sell_amount, 2),
            },
            "cash": {
                "inflow": round(inflow, 2),
                "outflow": round(outflow, 2),
                "net": round(inflow - outflow, 2),
            },
            "dividends": {"count": len(dividends), "items": dividends},
            "asset": {
                "begin_equity": begin_equity,
                "end_equity": end_equity,
                "return_pct": ret_pct,
            },
            "details": sorted(details, key=lambda d: d["date"]),
        }

    def build_equity_curve(
        self,
        *,
        days: int = 180,
        account_id: Optional[int] = None,
        cost_method: str = "fifo",
    ) -> Dict[str, Any]:
        """资产曲线：按日快照序列返回总资产/市值/现金与回撤。"""
        method = self._normalize_cost_method(cost_method)
        rows = self.repo.list_daily_snapshots_for_risk(
            as_of=date.today(),
            cost_method=method,
            account_id=account_id,
            lookback_days=int(days),
        )
        series: List[Dict[str, Any]] = []
        peak: Optional[float] = None
        max_dd = 0.0
        first_eq: Optional[float] = None
        last_eq: Optional[float] = None
        # 时间加权收益率（剔除外部入金/出金）
        cum_ret = 1.0
        prev_eq: Optional[float] = None
        for r in rows:
            eq = float(r.total_equity or 0)
            mv = float(r.total_market_value or 0)
            cash = float(r.total_cash or 0)
            if peak is None or eq > peak:
                peak = eq
            dd = (eq - peak) / peak * 100 if peak else 0.0
            if dd < max_dd:
                max_dd = dd
            if first_eq is None:
                first_eq = eq
            last_eq = eq
            # 时间加权：当日收益率 = (当日收盘权益 - 上日收盘权益 - 当日净入金) / 上日收盘权益
            if prev_eq is not None and prev_eq != 0:
                cf = 0.0
                try:
                    payload = json.loads(r.payload) if r.payload else {}
                    cf = float(payload.get("net_cashflow") or 0.0)
                except Exception:
                    cf = 0.0
                period_ret = (eq - prev_eq - cf) / prev_eq
                cum_ret *= (1 + period_ret)
            prev_eq = eq
            series.append({
                "date": r.snapshot_date.isoformat(),
                "total_equity": round(eq, 2),
                "total_market_value": round(mv, 2),
                "total_cash": round(cash, 2),
                "drawdown_pct": round(dd, 2),
            })
        ret_pct = None
        if first_eq and first_eq != 0 and last_eq is not None:
            ret_pct = round((cum_ret - 1) * 100, 2)
        return {
            "series": series,
            "summary": {
                "begin_equity": round(first_eq, 2) if first_eq is not None else None,
                "end_equity": round(last_eq, 2) if last_eq is not None else None,
                "return_pct": ret_pct,
                "max_drawdown_pct": round(max_dd, 2) if series else None,
                "points": len(series),
            },
        }

    def _replay_account(
        self,
        *,
        account: Any,
        as_of_date: date,
        cost_method: str,
        include_realtime: bool,
        snapshot_change_pct_map: Optional[Dict[str, Optional[float]]] = None,
        ths_price_map: Optional[Dict[str, float]] = None,
        ths_name_map: Optional[Dict[str, str]] = None,
    ) -> Dict[str, Any]:
        trades = self.repo.list_trades(account.id, as_of=as_of_date)
        cash_ledger = self.repo.list_cash_ledger(account.id, as_of=as_of_date)
        corporate_actions = self.repo.list_corporate_actions(account.id, as_of=as_of_date)

        events = []
        for row in cash_ledger:
            events.append(("cash", row.event_date, row.id, row))
        for row in trades:
            events.append(("trade", row.trade_date, row.id, row))
        for row in corporate_actions:
            events.append(("corp", row.effective_date, row.id, row))

        # Same-day deterministic ordering: cash -> corporate action -> trade.
        event_priority = {"cash": 0, "corp": 1, "trade": 2}
        events.sort(key=lambda item: (item[1], event_priority[item[0]], item[2]))

        cash_balances: Dict[str, float] = defaultdict(float)
        fees_total_base = 0.0
        taxes_total_base = 0.0
        realized_pnl_base = 0.0
        fx_stale = False

        fifo_lots: Dict[Tuple[str, str, str], List[Dict[str, Any]]] = defaultdict(list)
        avg_state: Dict[Tuple[str, str, str], _AvgState] = defaultdict(_AvgState)

        for event_type, event_date, _, event in events:
            if event_type == "cash":
                currency = self._normalize_currency(event.currency)
                amount = float(event.amount or 0.0)
                if event.direction == "in":
                    cash_balances[currency] += amount
                elif event.direction == "out":
                    cash_balances[currency] -= amount
                else:
                    raise ValueError(f"Unsupported cash direction: {event.direction}")
                continue

            if event_type == "trade":
                key = (
                    self._normalize_symbol_for_position(event.symbol),
                    self._normalize_market(event.market),
                    self._normalize_currency(event.currency),
                )
                qty = float(event.quantity or 0.0)
                price = float(event.price or 0.0)
                fee = float(event.fee or 0.0)
                tax = float(event.tax or 0.0)
                if qty <= 0 or price <= 0:
                    raise ValueError(f"Invalid trade quantity or price for {event.symbol}")

                gross = qty * price
                side = (event.side or "").lower().strip()
                if side == "buy":
                    cash_balances[key[2]] -= (gross + fee + tax)
                    if cost_method == "fifo":
                        unit_cost = (gross + fee + tax) / qty
                        fifo_lots[key].append(
                            {
                                "symbol": key[0],
                                "market": key[1],
                                "currency": key[2],
                                "open_date": event_date,
                                "remaining_quantity": qty,
                                "unit_cost": unit_cost,
                                "source_trade_id": event.id,
                            }
                        )
                    else:
                        state = avg_state[key]
                        state.quantity += qty
                        state.total_cost += (gross + fee + tax)
                elif side == "sell":
                    cash_balances[key[2]] += (gross - fee - tax)
                    proceeds_net = gross - fee - tax
                    if cost_method == "fifo":
                        cost_basis = self._consume_fifo_lots(
                            fifo_lots[key],
                            qty,
                            key[0],
                            event_date,
                        )
                    else:
                        cost_basis = self._consume_avg_position(
                            avg_state[key],
                            qty,
                            key[0],
                            event_date,
                        )
                    realized_local = proceeds_net - cost_basis
                    realized_base, stale_realized, _ = self._convert_amount(
                        amount=realized_local,
                        from_currency=key[2],
                        to_currency=account.base_currency,
                        as_of_date=event_date,
                    )
                    realized_pnl_base += realized_base
                    fx_stale = fx_stale or stale_realized
                else:
                    raise ValueError(f"Unsupported trade side: {event.side}")

                fee_base, stale_fee, _ = self._convert_amount(
                    amount=fee,
                    from_currency=key[2],
                    to_currency=account.base_currency,
                    as_of_date=event_date,
                )
                tax_base, stale_tax, _ = self._convert_amount(
                    amount=tax,
                    from_currency=key[2],
                    to_currency=account.base_currency,
                    as_of_date=event_date,
                )
                fees_total_base += fee_base
                taxes_total_base += tax_base
                fx_stale = fx_stale or stale_fee or stale_tax
                continue

            if event_type == "corp":
                key = (
                    self._normalize_symbol_for_position(event.symbol),
                    self._normalize_market(event.market),
                    self._normalize_currency(event.currency),
                )
                action_type = (event.action_type or "").strip().lower()
                if action_type == "cash_dividend":
                    per_share = float(event.cash_dividend_per_share or 0.0)
                    if per_share <= 0:
                        continue
                    qty_held = self._held_quantity(
                        key=key,
                        cost_method=cost_method,
                        fifo_lots=fifo_lots,
                        avg_state=avg_state,
                    )
                    if qty_held > EPS:
                        cash_balances[key[2]] += qty_held * per_share
                elif action_type == "split_adjustment":
                    split_ratio = float(event.split_ratio or 0.0)
                    if split_ratio <= 0:
                        raise ValueError(f"Invalid split_ratio for {event.symbol}")
                    if abs(split_ratio - 1.0) <= EPS:
                        continue
                    if cost_method == "fifo":
                        for lot in fifo_lots[key]:
                            lot["remaining_quantity"] *= split_ratio
                            lot["unit_cost"] /= split_ratio
                    else:
                        state = avg_state[key]
                        state.quantity *= split_ratio
                else:
                    raise ValueError(f"Unsupported corporate action type: {event.action_type}")

        position_rows, lot_rows, market_value_base, total_cost_base, stale_pos = self._build_positions(
            account=account,
            as_of_date=as_of_date,
            cost_method=cost_method,
            fifo_lots=fifo_lots,
            avg_state=avg_state,
            include_realtime=include_realtime,
            snapshot_change_pct_map=snapshot_change_pct_map,
            ths_price_map=ths_price_map,
            ths_name_map=ths_name_map,
        )
        fx_stale = fx_stale or stale_pos

        total_cash_base = 0.0
        for currency, amount in cash_balances.items():
            converted, stale, _ = self._convert_amount(
                amount=amount,
                from_currency=currency,
                to_currency=account.base_currency,
                as_of_date=as_of_date,
            )
            total_cash_base += converted
            fx_stale = fx_stale or stale

        unrealized_pnl_base = market_value_base - total_cost_base
        total_equity_base = total_cash_base + market_value_base
        # 今日盈亏（与同花顺账本「汇总持仓」口径一致）：
        # 昨收 = 现价 / (1 + 当日涨跌幅)，四舍五入到现价小数位；
        # 今日盈亏 = 数量 × (现价 - 昨收)。仅实时快照（含 day_change_pct）时有效。
        day_pnl = 0.0
        day_prev_market_value = 0.0
        for _position in position_rows:
            _mv = _position.get("market_value_base") or 0.0
            _dcp = _position.get("day_change_pct")
            _px = _position.get("last_price")
            _qty = _position.get("quantity")
            if _dcp is not None and _px:
                _dcp_f = float(_dcp)
                _px_f = float(_px)
                _qty_f = float(_qty or 0.0)
                _prev = _round_to_price_precision(_px_f / (1.0 + _dcp_f / 100.0), _px_f)
                day_pnl += round(_qty_f * (_px_f - _prev), 2)
                day_prev_market_value += _prev * _qty_f
            else:
                day_prev_market_value += _mv
        day_pnl_pct = (day_pnl / day_prev_market_value * 100.0) if day_prev_market_value > 0 else None
        unrealized_pnl_pct = (
            (unrealized_pnl_base / total_cost_base * 100.0) if abs(total_cost_base) > EPS else None
        )
        position_limitations = [
            limitation
            for position in position_rows
            for limitation in position.get("limitations", [])
        ]
        limitations = _merge_portfolio_limitations(
            _portfolio_limitations_for_market(account.market),
            position_limitations,
        )

        account_payload = {
            "account_id": account.id,
            "account_name": account.name,
            "owner_id": account.owner_id,
            "broker": account.broker,
            "market": account.market,
            "base_currency": account.base_currency,
            "as_of": as_of_date.isoformat(),
            "cost_method": cost_method,
            "total_cash": round(total_cash_base, 6),
            "total_market_value": round(market_value_base, 6),
            "total_equity": round(total_equity_base, 6),
            "realized_pnl": round(realized_pnl_base, 6),
            "unrealized_pnl": round(unrealized_pnl_base, 6),
            "unrealized_pnl_pct": round(unrealized_pnl_pct, 6) if unrealized_pnl_pct is not None else None,
            "day_pnl": round(day_pnl, 6),
            "day_pnl_pct": round(day_pnl_pct, 6) if day_pnl_pct is not None else None,
            "fee_total": round(fees_total_base, 6),
            "tax_total": round(taxes_total_base, 6),
            "fx_stale": fx_stale,
            "data_quality": "partial" if limitations else "ok",
            "limitations": limitations,
            "positions": position_rows,
        }

        return {
            "public": account_payload,
            "payload": account_payload,
            "positions_cache": position_rows,
            "lots_cache": lot_rows,
            "total_cash": float(total_cash_base),
            "total_market_value": float(market_value_base),
            "total_equity": float(total_equity_base),
            "realized_pnl": float(realized_pnl_base),
            "unrealized_pnl": float(unrealized_pnl_base),
            "fee_total": float(fees_total_base),
            "tax_total": float(taxes_total_base),
            "fx_stale": fx_stale,
        }

    def _build_positions(
        self,
        *,
        account: Any,
        as_of_date: date,
        cost_method: str,
        fifo_lots: Dict[Tuple[str, str, str], List[Dict[str, Any]]],
        avg_state: Dict[Tuple[str, str, str], _AvgState],
        include_realtime: bool = True,
        snapshot_change_pct_map: Optional[Dict[str, Optional[float]]] = None,
        ths_price_map: Optional[Dict[str, float]] = None,
        ths_name_map: Optional[Dict[str, str]] = None,
    ) -> Tuple[List[Dict[str, Any]], List[Dict[str, Any]], float, float, bool]:
        position_rows: List[Dict[str, Any]] = []
        lot_rows: List[Dict[str, Any]] = []
        market_value_base = 0.0
        total_cost_base = 0.0
        fx_stale = False

        keys: Iterable[Tuple[str, str, str]]
        if cost_method == "fifo":
            keys = list(fifo_lots.keys())
        else:
            keys = list(avg_state.keys())

        active_symbols: List[str] = []
        if include_realtime and as_of_date == date.today():
            for key in sorted(keys):
                symbol, _, _ = key
                if cost_method == "fifo":
                    qty = sum(
                        float(lot["remaining_quantity"])
                        for lot in fifo_lots[key]
                        if lot["remaining_quantity"] > EPS
                    )
                else:
                    qty = float(avg_state[key].quantity)
                if qty > EPS:
                    active_symbols.append(symbol)
        realtime_prices = (
            self._prefetch_realtime_position_prices(active_symbols)
            if active_symbols
            else None
        )

        for key in sorted(keys):
            symbol, market, currency = key

            if cost_method == "fifo":
                active_lots = [lot for lot in fifo_lots[key] if lot["remaining_quantity"] > EPS]
                qty = sum(float(lot["remaining_quantity"]) for lot in active_lots)
                if qty <= EPS:
                    continue
                total_cost = sum(float(lot["remaining_quantity"]) * float(lot["unit_cost"]) for lot in active_lots)
                avg_cost = total_cost / qty
                lot_rows.extend(active_lots)
            else:
                state = avg_state[key]
                qty = float(state.quantity)
                total_cost = float(state.total_cost)
                if qty <= EPS:
                    continue
                avg_cost = total_cost / qty
                lot_rows.append(
                    {
                        "symbol": symbol,
                        "market": market,
                        "currency": currency,
                        "open_date": as_of_date,
                        "remaining_quantity": qty,
                        "unit_cost": avg_cost,
                        "source_trade_id": None,
                    }
                )

            price_info = self._resolve_position_price(
                symbol=symbol,
                as_of_date=as_of_date,
                realtime_prices=realtime_prices,
                include_realtime=include_realtime,
            )
            last_price = price_info.price
            # 盘后不拉实时时，用同花顺账本最新价（今日收盘价）覆盖本地价格，
            # 保证市值/总资产反映当日收盘，且与账本网页一致。
            if (
                not include_realtime
                and ths_price_map is not None
                and symbol in ths_price_map
                and float(ths_price_map[symbol]) > 0
            ):
                last_price = float(ths_price_map[symbol])
                price_info = _ResolvedPositionPrice(
                    price=last_price,
                    source="ths_ledger",
                    price_date=date.today(),
                    is_stale=False,
                    is_available=True,
                    provider="ths_ledger",
                )
            limitations = _portfolio_limitations_for_market(market)

            if price_info.is_available:
                local_market_value = qty * float(last_price)
                market_base, stale_market, _ = self._convert_amount(
                    amount=local_market_value,
                    from_currency=currency,
                    to_currency=account.base_currency,
                    as_of_date=as_of_date,
                )
                cost_base, stale_cost, _ = self._convert_amount(
                    amount=total_cost,
                    from_currency=currency,
                    to_currency=account.base_currency,
                    as_of_date=as_of_date,
                )
                unrealized_base = market_base - cost_base
                fx_stale = fx_stale or stale_market or stale_cost
            else:
                market_base = 0.0
                cost_base = 0.0
                unrealized_base = 0.0

            unrealized_pct = None
            if abs(cost_base) > EPS:
                unrealized_pct = unrealized_base / cost_base * 100.0

            # 当日涨跌幅：
            # - 盘中实时来源：来自同一次实时行情缓存（不新增网络请求）；
            # - 盘后账本今收价来源：以「账本今收价 / 本地昨收 - 1」计算（与账本网页当日盈亏口径一致）；
            # - 其余场景：从已保存快照读回当日涨跌幅（避免今日盈亏丢失）。
            day_change_pct = None
            if as_of_date == date.today():
                if include_realtime and price_info.source == "realtime_quote":
                    day_change_pct = self._get_cached_realtime_change_pct(symbol)
                elif not include_realtime and price_info.source == "ths_ledger":
                    close = self.repo.get_latest_close_with_date(symbol=symbol, as_of=as_of_date)
                    if close and close[0] > 0:
                        prev = float(close[0])
                        if prev > 0:
                            day_change_pct = (last_price / prev - 1.0) * 100.0
                elif not include_realtime and snapshot_change_pct_map is not None and symbol in snapshot_change_pct_map:
                    day_change_pct = snapshot_change_pct_map[symbol]

            position_rows.append(
                {
                    "symbol": symbol,
                    "name": self._resolve_position_name(symbol, ths_name_map),
                    "market": market,
                    "currency": currency,
                    "quantity": round(qty, 8),
                    "avg_cost": round(avg_cost, 8),
                    "total_cost": round(total_cost, 8),
                    "last_price": round(float(last_price), 8),
                    "day_change_pct": round(day_change_pct, 8) if day_change_pct is not None else None,
                    "market_value_base": round(market_base, 8),
                    "unrealized_pnl_base": round(unrealized_base, 8),
                    "unrealized_pnl_pct": round(unrealized_pct, 8) if unrealized_pct is not None else None,
                    "valuation_currency": account.base_currency,
                    "price_source": price_info.source,
                    "price_provider": price_info.provider,
                    "price_date": price_info.price_date.isoformat() if price_info.price_date else None,
                    "price_stale": price_info.is_stale,
                    "price_available": price_info.is_available,
                    "data_quality": "partial" if limitations else "ok",
                    "limitations": limitations,
                }
            )

            market_value_base += market_base
            total_cost_base += cost_base

        return position_rows, lot_rows, market_value_base, total_cost_base, fx_stale

    @staticmethod
    def _resolve_position_name(symbol: str, ths_name_map: Optional[Dict[str, str]]) -> str:
        """解析持仓标的名称：优先账本接口名称（现成），回退本地名称映射，最后回退代码本身。"""
        if ths_name_map:
            name = ths_name_map.get(symbol)
            if name:
                return name
        try:
            from src.data.stock_mapping import STOCK_NAME_MAP

            name = STOCK_NAME_MAP.get(symbol)
            if name:
                return name
        except Exception:  # noqa: BLE001 - 名称是增强项，失败回退代码
            pass
        return symbol

    def _resolve_position_price(
        self,
        *,
        symbol: str,
        as_of_date: date,
        realtime_prices: Optional[Dict[str, Tuple[Optional[float], Optional[str]]]] = None,
        include_realtime: bool = True,
    ) -> _ResolvedPositionPrice:
        today = date.today()

        if include_realtime and as_of_date == today:
            if realtime_prices is None:
                realtime_price, provider = self._fetch_realtime_position_price(symbol)
            else:
                realtime_price, provider = realtime_prices.get(symbol, (None, None))
            if realtime_price is not None and realtime_price > 0:
                return _ResolvedPositionPrice(
                    price=float(realtime_price),
                    source="realtime_quote",
                    price_date=today,
                    is_stale=False,
                    is_available=True,
                    provider=provider,
                )

        close = self.repo.get_latest_close_with_date(symbol=symbol, as_of=as_of_date)
        if close is not None:
            close_price, close_date = close
            if close_price > 0:
                return _ResolvedPositionPrice(
                    price=float(close_price),
                    source="history_close",
                    price_date=close_date,
                    is_stale=close_date < as_of_date,
                    is_available=True,
                )

        return _ResolvedPositionPrice(
            price=0.0,
            source="missing",
            price_date=None,
            is_stale=True,
            is_available=False,
        )

    def _prefetch_realtime_position_prices(
        self,
        symbols: Iterable[str],
    ) -> Dict[str, Tuple[Optional[float], Optional[str]]]:
        unique_symbols = sorted({symbol for symbol in symbols if symbol})
        if not unique_symbols:
            return {}

        # Bulk prefetch (when applicable) only warms the fetcher-module-level realtime cache;
        # the manager itself is discarded so per-symbol workers cannot serialize through its
        # per-fetcher call locks when individual reads still need a live fetch (e.g. mixed
        # markets, cache miss, or bulk source returning fewer rows than requested).
        if len(unique_symbols) >= 5:
            # 若全部命中缓存则跳过实时抓取（避免 bulk prefetch 每次都拖慢页面加载）
            now_ts = time.time()
            ttl = PortfolioService._realtime_cache_ttl_seconds("cn")
            from src.core.trading_calendar import infer_market_phase

            try:
                phase = infer_market_phase("cn")
            except Exception:  # noqa: BLE001
                phase = None
            intraday_now = PortfolioService._is_intraday_phase(phase) if phase is not None else True
            all_cached = all(
                symbol in _REALTIME_PRICE_CACHE
                and now_ts - _REALTIME_PRICE_CACHE[symbol][0] < ttl
                and (_REALTIME_PRICE_CACHE[symbol][3] is None
                     or _REALTIME_PRICE_CACHE[symbol][3] == intraday_now)
                for symbol in unique_symbols
            )
            if not all_cached:
                try:
                    from data_provider.base import DataFetcherManager

                    DataFetcherManager().prefetch_realtime_quotes(unique_symbols)
                except Exception as exc:
                    logger.warning("Failed to prefetch realtime portfolio quotes: %s", exc)

        if len(unique_symbols) == 1:
            symbol = unique_symbols[0]
            return {symbol: self._fetch_realtime_position_price(symbol)}

        results: Dict[str, Tuple[Optional[float], Optional[str]]] = {}
        max_workers = min(PORTFOLIO_REALTIME_QUOTE_MAX_WORKERS, len(unique_symbols))
        with ThreadPoolExecutor(max_workers=max_workers, thread_name_prefix="portfolio-quote") as executor:
            futures = {
                executor.submit(self._fetch_realtime_position_price, symbol): symbol
                for symbol in unique_symbols
            }
            for future in as_completed(futures):
                symbol = futures[future]
                try:
                    results[symbol] = future.result()
                except Exception as exc:  # pragma: no cover - defensive guard for patched fetchers
                    logger.warning("Failed to prefetch realtime portfolio price for %s: %s", symbol, exc)
                    results[symbol] = (None, None)

        # 同花顺账本实时价交叉校验（best-effort）：腾讯价缺失时用账本价兜底；
        # 双方都有但差异明显时记录告警日志，便于发现行情源异常。
        try:
            ths_prices = self._fetch_ths_live_prices()
            if ths_prices:
                for symbol in list(results.keys()):
                    price, provider = results[symbol]
                    key = symbol.replace("SH", "").replace("SZ", "").replace("BJ", "").replace(".", "").zfill(6)
                    ths_price = ths_prices.get(key)
                    if ths_price and ths_price > 0:
                        if price is None:
                            results[symbol] = (ths_price, "ths_ledger")
                            logger.info("Portfolio price %s fallback to THS ledger %.4f", symbol, ths_price)
                        elif abs(price - ths_price) / ths_price > 0.01:
                            logger.warning(
                                "Portfolio price mismatch %s: tencent=%.4f vs ths=%.4f (%.2f%%)",
                                symbol,
                                price,
                                ths_price,
                                abs(price - ths_price) / ths_price * 100.0,
                            )
        except Exception as exc:  # noqa: BLE001 - 交叉校验是增强项，失败不影响主链路
            logger.warning("THS cross-check skipped: %s", exc)

        return results

    @staticmethod
    def _realtime_cache_ttl_seconds(market: Optional[str] = None) -> float:
        """按是否处于交易时段动态决定实时价缓存时长。

        - 交易时段（盘中 / 收盘集合竞价）：短 TTL（120s），每次刷新都能拿到最新价；
        - 非交易时段（盘前 / 午休 / 盘后 / 非交易日）：长 TTL（6 小时）。
          此时行情接口返回的就是最近收盘价，拉取一次后长时间复用，
          避免每次打开持仓页都调实时接口造成无效请求。
        """
        try:
            from src.core.trading_calendar import MarketPhase, infer_market_phase

            phase = infer_market_phase(market or "cn")
            if phase in (MarketPhase.INTRADAY, MarketPhase.CLOSING_AUCTION):
                return _REALTIME_PRICE_CACHE_TTL_SECONDS
            return 6 * 3600.0
        except Exception:  # fail-open: 无法判断时保持原实时行为
            return _REALTIME_PRICE_CACHE_TTL_SECONDS

    @staticmethod
    def _is_intraday_phase(phase: Any) -> bool:
        """判断市场阶段是否属于盘中（价格会持续变动）。"""
        from src.core.trading_calendar import MarketPhase

        return phase in (MarketPhase.INTRADAY, MarketPhase.CLOSING_AUCTION)

    @staticmethod
    def _fetch_realtime_position_price(symbol: str) -> Tuple[Optional[float], Optional[str]]:
        # 短 TTL 缓存命中则直接复用，避免每次页面刷新都实时抓取导致慢/失败
        now = time.time()
        try:
            from src.core.trading_calendar import get_market_for_stock, infer_market_phase

            market = get_market_for_stock(symbol)
            try:
                phase = infer_market_phase(market)
            except Exception:  # noqa: BLE001 - fail-open 默认按盘中处理
                phase = None
        except Exception:
            market = "cn"
            phase = None
        intraday_now = PortfolioService._is_intraday_phase(phase) if phase is not None else True
        ttl = PortfolioService._realtime_cache_ttl_seconds(market)
        cached = _REALTIME_PRICE_CACHE.get(symbol)
        if cached is not None and len(cached) >= 3:
            cached_ts = cached[0]
            cached_intraday = cached[3] if len(cached) > 3 else None
            # 跨阶段强制失效：盘中拉的价格不能复用于非盘中（收盘后必须重拉当日收盘价），
            # 反之非盘中（收盘价）也不应在次日盘中继续复用（盘中价格会变动）。
            phase_mismatch = cached_intraday is not None and cached_intraday != intraday_now
            if (now - cached_ts < ttl) and not phase_mismatch:
                return cached[1], cached[2]
        try:
            from data_provider.base import DataFetcherManager

            fetcher_manager = DataFetcherManager()
            quote = fetcher_manager.get_realtime_quote(symbol, log_final_failure=False)
        except Exception as exc:
            logger.warning("Failed to fetch realtime portfolio price for %s: %s", symbol, exc)
            return None, None

        if quote is None:
            return None, None

        price = getattr(quote, "price", None)
        try:
            numeric_price = float(price)
        except (TypeError, ValueError):
            return None, None

        if numeric_price <= 0:
            return None, None

        source = getattr(quote, "source", None)
        provider = getattr(source, "value", None) or (str(source) if source is not None else None)
        _REALTIME_PRICE_CACHE[symbol] = (now, numeric_price, provider, intraday_now)
        # 顺带缓存当日涨跌幅（同一 quote，避免重复请求）
        try:
            change_pct = float(getattr(quote, "change_pct", None))
        except (TypeError, ValueError):
            change_pct = None
        _REALTIME_CHANGE_PCT_CACHE[symbol] = (now, change_pct)
        return numeric_price, provider

    @staticmethod
    def _fetch_ths_live_prices() -> Dict[str, float]:
        """尽力而为地从同花顺账本拉取汇总持仓最新价（低频缓存，best-effort）。

        仅当同花顺登录态有效时才会真正请求账本接口；失败/未登录一律返回空 dict，
        不影响主价格链路（腾讯实时行情仍是主源）。账本价用于交叉校验与兜底。
        """
        now = time.time()
        entry = _THS_LIVE_PRICE_CACHE.get("prices")
        if entry is not None and now - entry[0] < _THS_LIVE_PRICE_TTL_SECONDS:
            return entry[1]
        prices: Dict[str, float] = {}
        try:
            from src.services.ths_sync.ths_sync_service import ThsSyncService

            svc = ThsSyncService()
            if not svc.client.is_logged_in():
                _THS_LIVE_PRICE_CACHE["prices"] = (now, prices)
                return prices
            merged = svc.fetch_merged()
            for p in merged.get("positions", []):
                code = str(p.get("code") or "").strip()
                price = float(p.get("price") or 0)
                if code and price > 0:
                    normalized = code.replace(".", "").zfill(6)
                    prices[normalized] = price
        except Exception as exc:  # noqa: BLE001 - 账本价是增强项，失败不影响主链路
            logger.warning("THS live price fetch failed: %s", exc)
        _THS_LIVE_PRICE_CACHE["prices"] = (now, prices)
        return prices

    @staticmethod
    def _get_cached_realtime_change_pct(symbol: str) -> Optional[float]:
        """读取实时行情当日涨跌幅缓存（仅读，不触发网络请求）。"""
        entry = _REALTIME_CHANGE_PCT_CACHE.get(symbol)
        if entry is None:
            return None
        ts, change_pct = entry
        if time.time() - ts > _REALTIME_PRICE_CACHE_TTL_SECONDS * 10:
            return None
        return change_pct

    @staticmethod
    def _normalize_symbol_for_storage(symbol: str) -> str:
        return canonical_stock_code(symbol)

    @staticmethod
    def _normalize_symbol_for_position(symbol: str) -> str:
        if not (symbol or "").strip():
            return ""

        raw = canonical_stock_code(symbol)
        if len(raw) >= 8 and raw[:2] in {"SH", "SZ", "BJ"} and raw[2:].isdigit():
            return raw

        if "." in raw:
            base, suffix = raw.rsplit(".", 1)
            if base.isdigit() and suffix in {"SH", "SS", "SZ", "BJ"}:
                exchange = "SH" if suffix == "SS" else suffix
                return f"{exchange}{base}"

        return canonical_stock_code(normalize_stock_code(symbol))

    @staticmethod
    def _normalize_symbol(symbol: str) -> str:
        """
        Canonicalization for symbol filtering with exchange-qualified input preservation.

        Keep explicit A-share exchange annotations (SH/SZ/BJ) intact to avoid collapsing
        different exchange variants of the same 6-digit core code.
        """
        raw = canonical_stock_code(symbol)
        if not raw:
            return ""

        if len(raw) >= 8 and raw[:2] in {"SH", "SZ", "BJ"} and raw[2:].isdigit():
            return raw

        if "." in raw:
            base, suffix = raw.rsplit(".", 1)
            if base.isdigit() and suffix in {"SH", "SS", "SZ", "BJ"}:
                exchange = "SH" if suffix == "SS" else suffix
                return f"{exchange}{base}"

        return canonical_stock_code(normalize_stock_code(symbol))

    @classmethod
    def _build_symbol_filter_values(cls, symbol: str) -> List[str]:
        original = (symbol or "").strip().upper()
        normalized = cls._normalize_symbol(original)
        if not normalized:
            return []

        seen: Set[str] = set()
        values: List[str] = []

        def _add(value: Optional[str]) -> None:
            candidate = (value or "").strip().upper()
            if candidate and candidate not in seen:
                seen.add(candidate)
                values.append(candidate)

        _add(original)
        _add(normalized)

        if normalized.startswith("HK"):
            hk_digits = normalized[2:]
            if hk_digits.isdigit() and len(hk_digits) == 5:
                legacy_hk_digits = str(int(hk_digits))
                _add(f"HK{hk_digits}")
                _add(f"HK{legacy_hk_digits}")
                _add(f"{hk_digits}.HK")
                _add(f"{legacy_hk_digits}.HK")
            return values

        explicit_exchange: Optional[str] = None
        if len(original) >= 8 and original[:2] in {"SH", "SZ", "BJ"} and original[2:].isdigit():
            explicit_exchange = original[:2]
            explicit_code = original[2:]
        elif "." in original:
            base, suffix = original.rsplit(".", 1)
            if base.isdigit() and suffix in {"SH", "SS", "SZ", "BJ"}:
                explicit_exchange = "SH" if suffix == "SS" else suffix
                explicit_code = base
            else:
                explicit_code = None
        else:
            explicit_code = None

        if normalized.isdigit():
            if len(normalized) == 6:
                exchanges = [explicit_exchange] if explicit_exchange else ["SH", "SZ", "BJ"]
                for exchange in exchanges:
                    if exchange is None:
                        continue
                    _add(f"{exchange}{normalized}")
                    _add(f"{normalized}.{'SS' if exchange == 'SH' else exchange}")
                    if exchange == "SH":
                        _add(f"{normalized}.SH")
            return values

        if explicit_exchange is not None and explicit_code is not None and explicit_code.isdigit():
            if len(explicit_code) == 6:
                _add(f"{explicit_exchange}{explicit_code}")
                _add(f"{explicit_code}.{'SS' if explicit_exchange == 'SH' else explicit_exchange}")
                if explicit_exchange == "SH":
                    _add(f"{explicit_code}.SH")
            elif len(normalized) == 5:
                _add(f"HK{normalized}")
                _add(f"{normalized}.HK")

        return values

    @staticmethod
    def _consume_fifo_lots(
        lots: List[Dict[str, Any]],
        quantity: float,
        symbol: str,
        trade_date: Optional[date] = None,
    ) -> float:
        remaining = quantity
        cost_basis = 0.0
        while remaining > EPS:
            if not lots:
                raise PortfolioOversellError(
                    symbol=symbol,
                    trade_date=trade_date,
                    requested_quantity=quantity,
                    available_quantity=quantity - remaining,
                )
            head = lots[0]
            take = min(remaining, float(head["remaining_quantity"]))
            cost_basis += take * float(head["unit_cost"])
            head["remaining_quantity"] = float(head["remaining_quantity"]) - take
            remaining -= take
            if head["remaining_quantity"] <= EPS:
                lots.pop(0)
        return cost_basis

    @staticmethod
    def _consume_avg_position(
        state: _AvgState,
        quantity: float,
        symbol: str,
        trade_date: Optional[date] = None,
    ) -> float:
        if state.quantity + EPS < quantity:
            raise PortfolioOversellError(
                symbol=symbol,
                trade_date=trade_date,
                requested_quantity=quantity,
                available_quantity=state.quantity,
            )
        if state.quantity <= EPS:
            raise PortfolioOversellError(
                symbol=symbol,
                trade_date=trade_date,
                requested_quantity=quantity,
                available_quantity=0.0,
            )
        avg_cost = state.total_cost / state.quantity
        cost_basis = avg_cost * quantity
        state.quantity -= quantity
        state.total_cost -= cost_basis
        if state.quantity <= EPS:
            state.quantity = 0.0
            state.total_cost = 0.0
        return cost_basis

    @staticmethod
    def _held_quantity(
        *,
        key: Tuple[str, str, str],
        cost_method: str,
        fifo_lots: Dict[Tuple[str, str, str], List[Dict[str, Any]]],
        avg_state: Dict[Tuple[str, str, str], _AvgState],
    ) -> float:
        if cost_method == "fifo":
            return sum(float(lot["remaining_quantity"]) for lot in fifo_lots.get(key, []))
        return float(avg_state.get(key, _AvgState()).quantity)

    def _convert_amount(
        self,
        *,
        amount: float,
        from_currency: str,
        to_currency: str,
        as_of_date: date,
    ) -> Tuple[float, bool, str]:
        from_norm = self._normalize_currency(from_currency)
        to_norm = self._normalize_currency(to_currency)
        if abs(amount) <= EPS:
            return 0.0, False, "zero"
        if from_norm == to_norm:
            return float(amount), False, "identity"

        direct = self.repo.get_latest_fx_rate(
            from_currency=from_norm,
            to_currency=to_norm,
            as_of=as_of_date,
        )
        if direct is not None and direct.rate > 0:
            return float(amount) * float(direct.rate), bool(direct.is_stale), "direct_rate"

        inverse = self.repo.get_latest_fx_rate(
            from_currency=to_norm,
            to_currency=from_norm,
            as_of=as_of_date,
        )
        if inverse is not None and inverse.rate > 0:
            return float(amount) / float(inverse.rate), bool(inverse.is_stale), "inverse_rate"

        # P0 fallback: keep pipeline available even when FX cache is missing.
        return float(amount), True, "fallback_1_to_1"

    def convert_amount(
        self,
        *,
        amount: float,
        from_currency: str,
        to_currency: str,
        as_of_date: date,
    ) -> Tuple[float, bool, str]:
        """Public conversion entry for cross-service consumers."""
        return self._convert_amount(
            amount=amount,
            from_currency=from_currency,
            to_currency=to_currency,
            as_of_date=as_of_date,
        )

    def _list_account_refresh_fx_currencies(
        self,
        *,
        account: Any,
        as_of_date: date,
        strict: bool = True,
    ) -> List[str]:
        """Return distinct non-base currencies participating in refresh for one account."""
        base_currency = self._normalize_currency(account.base_currency)
        currencies: Set[str] = set()
        rows = list(self.repo.list_trades(account.id, as_of=as_of_date))
        rows.extend(self.repo.list_cash_ledger(account.id, as_of=as_of_date))
        for row in rows:
            try:
                currency = self._normalize_currency(row.currency)
            except ValueError:
                if strict:
                    raise
                logger.warning(
                    "Skip invalid FX refresh currency for account %s on %s: %r",
                    account.id,
                    as_of_date.isoformat(),
                    getattr(row, "currency", None),
                )
                continue
            if currency != base_currency:
                currencies.add(currency)
        return sorted(currencies)

    def _refresh_account_fx_rates(
        self,
        *,
        account: Any,
        as_of_date: date,
        refresh_enabled: bool,
    ) -> Dict[str, int]:
        """Refresh FX pairs for one account and keep stale fallback on failures."""
        refresh_currencies = self._list_account_refresh_fx_currencies(
            account=account,
            as_of_date=as_of_date,
            strict=refresh_enabled,
        )
        if not refresh_enabled:
            return {
                "pair_count": len(refresh_currencies),
                "updated_count": 0,
                "stale_count": 0,
                "error_count": 0,
            }

        base_currency = self._normalize_currency(account.base_currency)
        summary = {
            "pair_count": len(refresh_currencies),
            "updated_count": 0,
            "stale_count": 0,
            "error_count": 0,
        }
        for from_currency in refresh_currencies:
            try:
                rate = self._fetch_fx_rate_from_yfinance(
                    from_currency=from_currency,
                    to_currency=base_currency,
                    as_of_date=as_of_date,
                )
                if rate is not None and rate > 0:
                    self.repo.save_fx_rate(
                        from_currency=from_currency,
                        to_currency=base_currency,
                        rate_date=as_of_date,
                        rate=rate,
                        source="yfinance",
                        is_stale=False,
                    )
                    summary["updated_count"] += 1
                    continue
            except Exception as exc:
                logger.warning(
                    "FX online fetch failed for %s/%s on %s: %s",
                    from_currency,
                    base_currency,
                    as_of_date.isoformat(),
                    exc,
                )

            fallback = self.repo.get_latest_fx_rate(
                from_currency=from_currency,
                to_currency=base_currency,
                as_of=as_of_date,
            )
            if fallback is not None and float(fallback.rate or 0.0) > 0:
                self.repo.save_fx_rate(
                    from_currency=from_currency,
                    to_currency=base_currency,
                    rate_date=as_of_date,
                    rate=float(fallback.rate),
                    source=(fallback.source or "cache_fallback"),
                    is_stale=True,
                )
                summary["stale_count"] += 1
            else:
                summary["error_count"] += 1
        return summary

    @staticmethod
    def _fetch_fx_rate_from_yfinance(
        *,
        from_currency: str,
        to_currency: str,
        as_of_date: date,
    ) -> Optional[float]:
        """Fetch latest available FX close rate around as_of date."""
        if yf is None:
            return None
        symbol = f"{from_currency}{to_currency}=X"
        ticker = yf.Ticker(symbol)
        history = ticker.history(
            start=(as_of_date - timedelta(days=7)).isoformat(),
            end=(as_of_date + timedelta(days=1)).isoformat(),
            interval="1d",
            auto_adjust=False,
        )
        if history is None or history.empty or "Close" not in history:
            return None
        close = history["Close"].dropna()
        if close.empty:
            return None
        value = float(close.iloc[-1])
        if value <= 0:
            return None
        return value

    def _require_active_account(self, account_id: int) -> Any:
        account = self.repo.get_account(account_id, include_inactive=False)
        if account is None:
            raise ValueError(f"Active account not found: {account_id}")
        return account

    def _require_active_account_in_session(self, *, session: Any, account_id: int) -> Any:
        account = self.repo.get_account_in_session(
            session=session,
            account_id=account_id,
            include_inactive=False,
        )
        if account is None:
            raise ValueError(f"Active account not found: {account_id}")
        return account

    def _has_trade_uid(self, *, account_id: int, trade_uid: str, session: Optional[Any] = None) -> bool:
        if session is None:
            return self.repo.has_trade_uid(account_id, trade_uid)
        return self.repo.has_trade_uid_in_session(session=session, account_id=account_id, trade_uid=trade_uid)

    def _has_trade_dedup_hash(
        self,
        *,
        account_id: int,
        dedup_hash: str,
        session: Optional[Any] = None,
    ) -> bool:
        if session is None:
            return self.repo.has_trade_dedup_hash(account_id, dedup_hash)
        return self.repo.has_trade_dedup_hash_in_session(
            session=session,
            account_id=account_id,
            dedup_hash=dedup_hash,
        )

    @staticmethod
    def _account_to_dict(row: Any) -> Dict[str, Any]:
        return {
            "id": row.id,
            "owner_id": row.owner_id,
            "name": row.name,
            "broker": row.broker,
            "market": row.market,
            "base_currency": row.base_currency,
            "is_active": bool(row.is_active),
            "created_at": row.created_at.isoformat() if row.created_at else None,
            "updated_at": row.updated_at.isoformat() if row.updated_at else None,
        }

    @staticmethod
    def _trade_row_to_dict(row: Any) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "account_id": int(row.account_id),
            "trade_uid": row.trade_uid,
            "symbol": row.symbol,
            "market": row.market,
            "currency": row.currency,
            "trade_date": row.trade_date.isoformat() if row.trade_date else "",
            "side": row.side,
            "quantity": float(row.quantity),
            "price": float(row.price),
            "fee": float(row.fee),
            "tax": float(row.tax),
            "note": row.note,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    @staticmethod
    def _cash_ledger_row_to_dict(row: Any) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "account_id": int(row.account_id),
            "event_date": row.event_date.isoformat() if row.event_date else "",
            "direction": row.direction,
            "amount": float(row.amount),
            "currency": row.currency,
            "note": row.note,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    @staticmethod
    def _corporate_action_row_to_dict(row: Any) -> Dict[str, Any]:
        return {
            "id": int(row.id),
            "account_id": int(row.account_id),
            "symbol": row.symbol,
            "market": row.market,
            "currency": row.currency,
            "effective_date": row.effective_date.isoformat() if row.effective_date else "",
            "action_type": row.action_type,
            "cash_dividend_per_share": (
                float(row.cash_dividend_per_share) if row.cash_dividend_per_share is not None else None
            ),
            "split_ratio": float(row.split_ratio) if row.split_ratio is not None else None,
            "note": row.note,
            "created_at": row.created_at.isoformat() if row.created_at else None,
        }

    @staticmethod
    def _validate_paging(*, page: int, page_size: int) -> Tuple[int, int]:
        if page < 1:
            raise ValueError("page must be >= 1")
        if page_size < 1 or page_size > 100:
            raise ValueError("page_size must be in [1, 100]")
        return page, page_size

    @staticmethod
    def _normalize_market(value: str) -> str:
        market = (value or "").strip().lower()
        if market not in VALID_MARKETS:
            raise ValueError("market must be one of: cn, hk, us, jp, kr, tw")
        return market

    @staticmethod
    def _normalize_currency(value: str) -> str:
        currency = (value or "").strip().upper()
        if not currency:
            raise ValueError("currency is required")
        return currency

    @staticmethod
    def _normalize_cost_method(value: str) -> str:
        method = (value or "").strip().lower()
        if method not in VALID_COST_METHODS:
            raise ValueError("cost_method must be fifo or avg")
        return method

    @staticmethod
    def _default_currency_for_market(market: str) -> str:
        if market == "hk":
            return "HKD"
        if market == "us":
            return "USD"
        return "CNY"
