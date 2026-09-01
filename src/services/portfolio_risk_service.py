# -*- coding: utf-8 -*-
"""Portfolio risk service for concentration, drawdown and stop-loss proximity."""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from datetime import date, timedelta
from typing import Any, Dict, List, Optional, Tuple

from src.config import Config, get_config
from src.repositories.portfolio_repo import PortfolioRepository
from src.services.decision_signal_service import DecisionSignalService
from src.services.decision_signal_summary import summarize_decision_signal
from src.services.portfolio_service import PortfolioService

logger = logging.getLogger(__name__)

DEFENSIVE_DECISION_SIGNAL_ACTIONS = ("sell", "reduce", "alert")

# ---------------------------------------------------------------------------
# 行业识别：ETF 内置映射 + 名称关键词推断 + 磁盘持久缓存
# ETF 没有东财所属行业板块（get_belong_boards 返回空），需要单独识别。
# ---------------------------------------------------------------------------

# 常见 A 股 ETF 代码 -> 申万一级行业（未收录的走名称关键词推断）
_ETF_SECTOR_MAP: Dict[str, str] = {
    # 行业/主题 ETF
    "512400": "有色金属",
    "159876": "有色金属",
    "512880": "非银金融",
    "512000": "非银金融",
    "512800": "银行",
    "512700": "银行",
    "515290": "银行",
    "512010": "医药生物",
    "512170": "医药生物",
    "512290": "医药生物",
    "159929": "医药生物",
    "512690": "食品饮料",
    "159928": "食品饮料",
    "159928": "食品饮料",
    "512200": "房地产",
    "512660": "国防军工",
    "512960": "国防军工",
    "512710": "国防军工",
    "512480": "电子",
    "512760": "电子",
    "159995": "电子",
    "159813": "电子",
    "515050": "计算机",
    "512720": "计算机",
    "159851": "计算机",
    "515230": "计算机",
    "515880": "通信",
    "516510": "计算机",
    "515790": "电力设备",
    "515030": "汽车",
    "516160": "电力设备",
    "159755": "电力设备",
    "159825": "农林牧渔",
    "159865": "农林牧渔",
    "516510": "计算机",
    "515220": "煤炭",
    "515210": "钢铁",
    "159870": "基础化工",
    "516020": "基础化工",
    "159928": "食品饮料",
    "512980": "传媒",
    "159869": "传媒",
    "515180": "公用事业",
    "159611": "电力",
    "512580": "环保",
    "512390": "有色金属",
    "159941": "有色金属",
    "518880": "有色金属",
    "518800": "有色金属",
    # 宽基（归入对应宽基行业口径，集中度展示用）
    "510050": "宽基·上证50",
    "510300": "宽基·沪深300",
    "159919": "宽基·沪深300",
    "510500": "宽基·中证500",
    "159922": "宽基·中证500",
    "512100": "宽基·中证1000",
    "588000": "宽基·科创50",
    "159915": "宽基·创业板",
    "510880": "宽基·红利",
    "515080": "宽基·红利",
    "510310": "宽基·沪深300",
    "512090": "宽基·MSCI",
    "159912": "宽基·深证100",
    "510510": "宽基·中证500",
    "512500": "宽基·中证500",
}

# ETF 名称关键词 -> 行业（映射表未命中时，从 ETF 名称推断）
_ETF_SECTOR_KEYWORDS: List[Tuple[str, str]] = [
    ("有色金属", "有色金属"),
    ("有色", "有色金属"),
    ("稀土", "有色金属"),
    ("黄金", "有色金属"),
    ("稀有金属", "有色金属"),
    ("军工", "国防军工"),
    ("国防", "国防军工"),
    ("半导体", "电子"),
    ("芯片", "电子"),
    ("电子", "电子"),
    ("集成电路", "电子"),
    ("计算机", "计算机"),
    ("软件", "计算机"),
    ("云计算", "计算机"),
    ("大数据", "计算机"),
    ("人工智能", "计算机"),
    ("机器人", "机械设备"),
    ("机械", "机械设备"),
    ("通信", "通信"),
    ("5G", "通信"),
    ("银行", "银行"),
    ("证券", "非银金融"),
    ("券商", "非银金融"),
    ("保险", "非银金融"),
    ("金融", "非银金融"),
    ("医药", "医药生物"),
    ("医疗", "医药生物"),
    ("生物", "医药生物"),
    ("创新药", "医药生物"),
    ("白酒", "食品饮料"),
    ("酒", "食品饮料"),
    ("食品", "食品饮料"),
    ("饮料", "食品饮料"),
    ("消费", "食品饮料"),
    ("家电", "家用电器"),
    ("房地产", "房地产"),
    ("地产", "房地产"),
    ("农业", "农林牧渔"),
    ("养殖", "农林牧渔"),
    ("畜牧", "农林牧渔"),
    ("粮食", "农林牧渔"),
    ("光伏", "电力设备"),
    ("新能源", "电力设备"),
    ("电池", "电力设备"),
    ("储能", "电力设备"),
    ("风电", "电力设备"),
    ("电力", "公用事业"),
    ("公用事业", "公用事业"),
    ("煤炭", "煤炭"),
    ("能源", "煤炭"),
    ("石油", "石油石化"),
    ("油气", "石油石化"),
    ("化工", "基础化工"),
    ("钢铁", "钢铁"),
    ("环保", "环保"),
    ("传媒", "传媒"),
    ("游戏", "传媒"),
    ("影视", "传媒"),
    ("基建", "建筑装饰"),
    ("建筑", "建筑装饰"),
    ("工程", "建筑装饰"),
    ("物流", "交通运输"),
    ("运输", "交通运输"),
    ("红利", "宽基·红利"),
    ("上证50", "宽基·上证50"),
    ("沪深300", "宽基·沪深300"),
    ("中证500", "宽基·中证500"),
    ("中证1000", "宽基·中证1000"),
    ("科创", "宽基·科创"),
    ("创业板", "宽基·创业板"),
    ("MSCI", "宽基·MSCI"),
    ("标普", "宽基·海外"),
    ("纳指", "宽基·海外"),
    ("恒生", "宽基·海外"),
    ("恒生科技", "宽基·海外"),
]

# 行业解析磁盘缓存（板块数据实时拉取很慢，缓存 7 天）
_SECTOR_CACHE_FILE = None  # 延迟初始化
_SECTOR_CACHE_TTL_SECONDS = 7 * 24 * 3600
_sector_disk_cache: Dict[str, Dict[str, Any]] = {}
_sector_disk_cache_lock = None  # 延迟初始化
_sector_disk_cache_loaded = False

# ETF 名称内存缓存（避免重复拉实时行情）
_etf_name_cache: Dict[str, str] = {}
_etf_name_cache_ts: Dict[str, float] = {}


def _sector_cache_path() -> str:
    global _SECTOR_CACHE_FILE
    if _SECTOR_CACHE_FILE is None:
        config = get_config()
        data_dir = getattr(config, "data_dir", None) or "data"
        _SECTOR_CACHE_FILE = os.path.join(data_dir, "cache", "portfolio_sector_cache.json")
    return _SECTOR_CACHE_FILE


def _load_sector_disk_cache() -> None:
    global _sector_disk_cache, _sector_disk_cache_loaded, _sector_disk_cache_lock
    if _sector_disk_cache_loaded:
        return
    _sector_disk_cache_loaded = True
    if _sector_disk_cache_lock is None:
        _sector_disk_cache_lock = threading.Lock()
    path = _sector_cache_path()
    try:
        if os.path.exists(path):
            with open(path, "r", encoding="utf-8") as f:
                _sector_disk_cache = json.load(f)
    except Exception as exc:  # pragma: no cover
        logger.warning("读取行业缓存失败: %s", exc)
        _sector_disk_cache = {}


def _save_sector_disk_cache() -> None:
    path = _sector_cache_path()
    try:
        os.makedirs(os.path.dirname(path), exist_ok=True)
        tmp = path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump(_sector_disk_cache, f, ensure_ascii=False)
        os.replace(tmp, path)
    except Exception as exc:  # pragma: no cover
        logger.warning("写入行业缓存失败: %s", exc)


def _get_cached_sector(symbol: str) -> Optional[str]:
    now = time.time()
    entry = _sector_disk_cache.get(symbol)
    if not entry:
        return None
    if now - float(entry.get("ts", 0)) > _SECTOR_CACHE_TTL_SECONDS:
        return None
    return entry.get("sector")


def _set_cached_sector(symbol: str, sector: str) -> None:
    _sector_disk_cache[symbol] = {"sector": sector, "ts": time.time()}
    _save_sector_disk_cache()


def _is_etf_symbol(symbol: str) -> bool:
    code = symbol.strip().split(".")[0]
    return len(code) == 6 and code.startswith(("51", "52", "56", "58", "15", "16", "18"))


def _infer_etf_sector_from_name(name: str) -> Optional[str]:
    if not name:
        return None
    for keyword, sector in _ETF_SECTOR_KEYWORDS:
        if keyword in name:
            return sector
    return None


class PortfolioRiskService:
    """Compute portfolio risk blocks on top of replayed snapshot data."""

    def __init__(
        self,
        *,
        repo: Optional[PortfolioRepository] = None,
        portfolio_service: Optional[PortfolioService] = None,
        decision_signal_service: Optional[DecisionSignalService] = None,
        config: Optional[Config] = None,
    ):
        self.repo = repo or PortfolioRepository()
        self.portfolio_service = portfolio_service or PortfolioService(repo=self.repo)
        self.decision_signal_service = decision_signal_service or DecisionSignalService(portfolio_repo=self.repo)
        self.config = config or get_config()
        self._data_manager = None
        self._data_manager_init_error = ""

    def get_risk_report(
        self,
        *,
        account_id: Optional[int] = None,
        as_of: Optional[date] = None,
        cost_method: str = "fifo",
        include_realtime: bool = True,
    ) -> Dict[str, Any]:
        as_of_date = as_of or date.today()
        snapshot = self.portfolio_service.get_portfolio_snapshot(
            account_id=account_id,
            as_of=as_of_date,
            cost_method=cost_method,
            include_realtime=include_realtime,
        )

        thresholds = {
            "concentration_alert_pct": float(getattr(self.config, "portfolio_risk_concentration_alert_pct", 35.0)),
            "drawdown_alert_pct": float(getattr(self.config, "portfolio_risk_drawdown_alert_pct", 15.0)),
            "stop_loss_alert_pct": float(getattr(self.config, "portfolio_risk_stop_loss_alert_pct", 10.0)),
            "stop_loss_near_ratio": float(getattr(self.config, "portfolio_risk_stop_loss_near_ratio", 0.8)),
            "lookback_days": int(getattr(self.config, "portfolio_risk_lookback_days", 180)),
        }

        concentration = self._build_concentration(
            snapshot,
            thresholds["concentration_alert_pct"],
            as_of_date=as_of_date,
        )
        sector_concentration = self._build_sector_concentration(
            snapshot,
            thresholds["concentration_alert_pct"],
            as_of_date=as_of_date,
        )
        self._ensure_drawdown_snapshot_window(
            account_id=account_id,
            as_of_date=as_of_date,
            cost_method=cost_method,
            lookback_days=thresholds["lookback_days"],
            include_realtime=include_realtime,
        )
        drawdown = self._build_drawdown(
            account_id=account_id,
            as_of_date=as_of_date,
            cost_method=cost_method,
            threshold_pct=thresholds["drawdown_alert_pct"],
            lookback_days=thresholds["lookback_days"],
        )
        stop_loss = self._build_stop_loss(snapshot, thresholds)
        decision_signal_risk = self._build_decision_signal_risk(snapshot)

        return {
            "as_of": as_of_date.isoformat(),
            "account_id": account_id,
            "cost_method": cost_method,
            "currency": snapshot["currency"],
            "thresholds": thresholds,
            "concentration": concentration,
            "sector_concentration": sector_concentration,
            "drawdown": drawdown,
            "stop_loss": stop_loss,
            "decision_signal_risk": decision_signal_risk,
        }

    def _build_decision_signal_risk(
        self,
        snapshot: Dict[str, Any],
    ) -> Dict[str, Any]:
        try:
            held_positions = self._held_position_identities(snapshot)
            if not held_positions:
                return self._empty_decision_signal_risk(available=True)
            stock_identities = sorted({
                (position["market"], position["signal_stock_code"])
                for position in held_positions
            })

            defensive_actions = set(DEFENSIVE_DECISION_SIGNAL_ACTIONS)
            latest_by_identity: Dict[Tuple[str, str], Dict[str, Any]] = {}
            page = 1
            while True:
                response = self.decision_signal_service.list_signals(
                    stock_identities=stock_identities,
                    status="active",
                    page=page,
                    page_size=100,
                )
                items = response.get("items", []) if isinstance(response, dict) else []
                for item in items:
                    if str(item.get("action") or "") not in defensive_actions:
                        continue
                    key = (
                        str(item.get("market") or "").strip().lower(),
                        str(item.get("stock_code") or "").strip().upper(),
                    )
                    if key[0] and key[1] and key not in latest_by_identity:
                        latest_by_identity[key] = item
                total = int(response.get("total", 0) or 0) if isinstance(response, dict) else 0
                if page * 100 >= total or not items:
                    break
                page += 1

            risk_items: List[Dict[str, Any]] = []
            action_counts = {action: 0 for action in DEFENSIVE_DECISION_SIGNAL_ACTIONS}
            seen: set[Tuple[Optional[int], str, str, int]] = set()
            for position in held_positions:
                signal = latest_by_identity.get((position["market"], position["signal_stock_code"]))
                summary = summarize_decision_signal(signal)
                if not summary:
                    continue
                action = str(summary.get("action") or "")
                if action not in action_counts:
                    continue
                signal_id = int(summary.get("id") or 0)
                dedupe_key = (position["account_id"], position["market"], position["signal_stock_code"], signal_id)
                if dedupe_key in seen:
                    continue
                seen.add(dedupe_key)
                action_counts[action] += 1
                risk_items.append({
                    "account_id": position["account_id"],
                    "symbol": position["symbol"],
                    "market": position["market"],
                    "signal": summary,
                })

            return {
                "available": True,
                "total": len(risk_items),
                "actions": action_counts,
                "items": risk_items,
            }
        except Exception:
            logger.exception("[PortfolioRiskService] Decision signal risk unavailable")
            return self._empty_decision_signal_risk(available=False)

    @staticmethod
    def _empty_decision_signal_risk(*, available: bool) -> Dict[str, Any]:
        return {
            "available": available,
            "total": 0,
            "actions": {action: 0 for action in DEFENSIVE_DECISION_SIGNAL_ACTIONS},
            "items": [],
        }

    @staticmethod
    def _held_position_identities(snapshot: Dict[str, Any]) -> List[Dict[str, Any]]:
        positions: List[Dict[str, Any]] = []
        for account in snapshot.get("accounts", []) or []:
            account_id = account.get("account_id")
            for pos in account.get("positions", []) or []:
                symbol = str(pos.get("symbol") or "").strip().upper()
                market = str(pos.get("market") or "").strip().lower()
                if not symbol or market not in {"cn", "hk", "us"}:
                    continue
                signal_stock_code = DecisionSignalService.normalize_stock_code_for_signal(symbol, market=market)
                positions.append({
                    "account_id": account_id,
                    "symbol": symbol,
                    "market": market,
                    "signal_stock_code": signal_stock_code,
                })
        return positions

    def _ensure_drawdown_snapshot_window(
        self,
        *,
        account_id: Optional[int],
        as_of_date: date,
        cost_method: str,
        lookback_days: int,
        include_realtime: bool,
    ) -> None:
        if lookback_days <= 0:
            return

        start_date = self._resolve_backfill_start_date(
            account_id=account_id,
            as_of_date=as_of_date,
            lookback_days=lookback_days,
        )
        if start_date > as_of_date:
            return

        existing_rows = self.repo.list_daily_snapshots_for_risk(
            as_of=as_of_date,
            cost_method=cost_method,
            account_id=account_id,
            lookback_days=lookback_days,
        )
        if account_id is not None:
            existing_dates = {row.snapshot_date for row in existing_rows if int(row.account_id) == int(account_id)}
            current_date = start_date
            while current_date <= as_of_date:
                if current_date.weekday() >= 5:  # 跳过周末（无行情，避免市值=0 假快照）
                    current_date += timedelta(days=1)
                    continue
                if current_date not in existing_dates:
                    self.portfolio_service.get_portfolio_snapshot(
                        account_id=account_id,
                        as_of=current_date,
                        cost_method=cost_method,
                        include_realtime=include_realtime,
                    )
                    existing_dates.add(current_date)
                current_date += timedelta(days=1)
            return

        account_ids = [int(account.id) for account in self.repo.list_accounts(include_inactive=False)]
        if not account_ids:
            return
        existing_pairs = {(int(row.account_id), row.snapshot_date) for row in existing_rows}
        current_date = start_date
        while current_date <= as_of_date:
            if current_date.weekday() >= 5:  # 跳过周末（无行情，避免市值=0 假快照）
                current_date += timedelta(days=1)
                continue
            if not all((aid, current_date) in existing_pairs for aid in account_ids):
                self.portfolio_service.get_portfolio_snapshot(
                    account_id=None,
                    as_of=current_date,
                    cost_method=cost_method,
                    include_realtime=include_realtime,
                )
                for aid in account_ids:
                    existing_pairs.add((aid, current_date))
            current_date += timedelta(days=1)

    def _resolve_backfill_start_date(
        self,
        *,
        account_id: Optional[int],
        as_of_date: date,
        lookback_days: int,
    ) -> date:
        window_start = as_of_date - timedelta(days=lookback_days)
        if account_id is not None:
            first_activity = self.repo.get_first_activity_date(account_id=account_id, as_of=as_of_date)
            return max(window_start, first_activity or as_of_date)

        first_activity_candidates: List[date] = []
        for account in self.repo.list_accounts(include_inactive=False):
            first_activity = self.repo.get_first_activity_date(account_id=int(account.id), as_of=as_of_date)
            if first_activity is not None:
                first_activity_candidates.append(first_activity)
        if not first_activity_candidates:
            return as_of_date
        return max(window_start, min(first_activity_candidates))

    def _build_concentration(self, snapshot: Dict[str, Any], threshold_pct: float, *, as_of_date: date) -> Dict[str, Any]:
        total_mv = float(snapshot.get("total_market_value", 0.0) or 0.0)
        exposure_by_symbol: Dict[str, float] = {}
        for account in snapshot.get("accounts", []):
            for pos in account.get("positions", []):
                symbol = str(pos.get("symbol") or "").strip().upper()
                if not symbol:
                    continue
                market_value = float(pos.get("market_value_base") or 0.0)
                valuation_currency = str(pos.get("valuation_currency") or account.get("base_currency") or "CNY")
                converted, _, _ = self.portfolio_service.convert_amount(
                    amount=market_value,
                    from_currency=valuation_currency,
                    to_currency="CNY",
                    as_of_date=as_of_date,
                )
                exposure_by_symbol[symbol] = exposure_by_symbol.get(symbol, 0.0) + converted

        rows = []
        for symbol, exposure in exposure_by_symbol.items():
            weight = (exposure / total_mv * 100.0) if total_mv > 0 else 0.0
            rows.append(
                {
                    "symbol": symbol,
                    "market_value_base": round(exposure, 6),
                    "weight_pct": round(weight, 4),
                    "is_alert": bool(weight >= threshold_pct),
                }
            )
        rows.sort(key=lambda item: item["market_value_base"], reverse=True)

        top_weight = rows[0]["weight_pct"] if rows else 0.0
        return {
            "total_market_value": round(total_mv, 6),
            "top_weight_pct": round(float(top_weight), 4),
            "alert": bool(top_weight >= threshold_pct),
            "top_positions": rows[:10],
        }

    def _build_sector_concentration(
        self,
        snapshot: Dict[str, Any],
        threshold_pct: float,
        *,
        as_of_date: date,
    ) -> Dict[str, Any]:
        total_mv = float(snapshot.get("total_market_value", 0.0) or 0.0)
        sector_exposure: Dict[str, float] = {}
        sector_symbols: Dict[str, set] = {}
        coverage = {
            "classified_count": 0,
            "unclassified_count": 0,
            "failed_count": 0,
        }
        errors: List[str] = []
        board_cache: Dict[Tuple[str, str], str] = {}

        for account in snapshot.get("accounts", []):
            for pos in account.get("positions", []):
                symbol = str(pos.get("symbol") or "").strip().upper()
                market = str(pos.get("market") or account.get("market") or "").strip().lower()
                if not symbol:
                    continue

                market_value = float(pos.get("market_value_base") or 0.0)
                valuation_currency = str(pos.get("valuation_currency") or account.get("base_currency") or "CNY")
                converted, _, _ = self.portfolio_service.convert_amount(
                    amount=market_value,
                    from_currency=valuation_currency,
                    to_currency="CNY",
                    as_of_date=as_of_date,
                )

                sector = self._resolve_primary_sector(
                    symbol=symbol,
                    market=market,
                    board_cache=board_cache,
                    coverage=coverage,
                    errors=errors,
                )
                sector_exposure[sector] = sector_exposure.get(sector, 0.0) + converted
                sector_symbols.setdefault(sector, set()).add(symbol)

        rows = []
        for sector, exposure in sector_exposure.items():
            weight = (exposure / total_mv * 100.0) if total_mv > 0 else 0.0
            rows.append(
                {
                    "sector": sector,
                    "market_value_base": round(exposure, 6),
                    "weight_pct": round(weight, 4),
                    "symbol_count": len(sector_symbols.get(sector, set())),
                    "is_alert": bool(weight >= threshold_pct),
                }
            )
        rows.sort(key=lambda item: item["market_value_base"], reverse=True)
        top_weight = rows[0]["weight_pct"] if rows else 0.0

        return {
            "total_market_value": round(total_mv, 6),
            "top_weight_pct": round(float(top_weight), 4),
            "alert": bool(top_weight >= threshold_pct),
            "top_sectors": rows[:10],
            "coverage": coverage,
            "errors": errors[:20],
        }

    def _resolve_primary_sector(
        self,
        *,
        symbol: str,
        market: str,
        board_cache: Dict[Tuple[str, str], str],
        coverage: Dict[str, int],
        errors: List[str],
    ) -> str:
        cache_key = (symbol, market)
        if cache_key in board_cache:
            return board_cache[cache_key]

        if market != "cn":
            coverage["unclassified_count"] += 1
            board_cache[cache_key] = "UNCLASSIFIED"
            return board_cache[cache_key]

        # 磁盘持久缓存（避免每次打开页面都实时拉板块，降低 20s+ 延迟）
        _load_sector_disk_cache()
        cached = _get_cached_sector(symbol)
        if cached:
            coverage["classified_count" if cached != "UNCLASSIFIED" else "unclassified_count"] += 1
            board_cache[cache_key] = cached
            return cached

        # ETF 没有东财行业板块，用内置映射 + 名称关键词推断识别行业
        if _is_etf_symbol(symbol):
            sector = self._resolve_etf_sector(symbol)
            if sector:
                coverage["classified_count"] += 1
                board_cache[cache_key] = sector
                _set_cached_sector(symbol, sector)
                return sector
            coverage["unclassified_count"] += 1
            board_cache[cache_key] = "UNCLASSIFIED"
            _set_cached_sector(symbol, "UNCLASSIFIED")
            return board_cache[cache_key]

        try:
            boards = self._fetch_belong_boards(symbol)
            sector_name = self._pick_primary_board_name(boards)
            if sector_name:
                coverage["classified_count"] += 1
                board_cache[cache_key] = sector_name
                _set_cached_sector(symbol, sector_name)
                return board_cache[cache_key]
        except Exception as exc:
            coverage["failed_count"] += 1
            errors.append(f"{symbol}: {exc}")

        coverage["unclassified_count"] += 1
        board_cache[cache_key] = "UNCLASSIFIED"
        _set_cached_sector(symbol, "UNCLASSIFIED")
        return board_cache[cache_key]

    def _resolve_etf_sector(self, symbol: str) -> Optional[str]:
        """识别 ETF 所属行业：内置映射优先，其次按 ETF 名称关键词推断。"""
        # 1. 内置映射
        mapped = _ETF_SECTOR_MAP.get(symbol)
        if mapped:
            return mapped

        # 2. 名称关键词推断（名称从实时行情获取，带内存缓存）
        name = self._fetch_etf_name(symbol)
        if name:
            return _infer_etf_sector_from_name(name)
        return None

    def _fetch_etf_name(self, symbol: str) -> Optional[str]:
        now = time.time()
        if symbol in _etf_name_cache and now - _etf_name_cache_ts.get(symbol, 0) < 6 * 3600:
            return _etf_name_cache[symbol]
        try:
            manager = self._get_data_manager()
            if manager is None:
                return None
            quote = manager.get_realtime_quote(symbol, log_final_failure=False)
            name = None
            if quote is not None:
                name = str(getattr(quote, "name", "") or "").strip()
            _etf_name_cache[symbol] = name or ""
            _etf_name_cache_ts[symbol] = now
            return name
        except Exception as exc:  # pragma: no cover - 名称获取失败不影响分类兜底
            logger.warning("获取 ETF %s 名称失败: %s", symbol, exc)
            _etf_name_cache[symbol] = ""
            _etf_name_cache_ts[symbol] = now
            return None

    def _fetch_belong_boards(self, symbol: str) -> List[Dict[str, Any]]:
        manager = self._get_data_manager()
        if manager is None:
            return []
        result = manager.get_belong_boards(symbol)
        if isinstance(result, list):
            return result
        return []

    @staticmethod
    def _pick_primary_board_name(boards: List[Dict[str, Any]]) -> Optional[str]:
        if not boards:
            return None

        preferred: Optional[str] = None
        fallback: Optional[str] = None
        for item in boards:
            if not isinstance(item, dict):
                continue
            name = str(item.get("name") or "").strip()
            if not name:
                continue
            if fallback is None:
                fallback = name
            type_text = str(item.get("type") or "").strip().lower()
            if "行业" in type_text or "industry" in type_text:
                preferred = name
                break
        return preferred or fallback

    def _get_data_manager(self):
        if self._data_manager is not None:
            return self._data_manager
        if self._data_manager_init_error:
            return None
        try:
            from data_provider import DataFetcherManager

            self._data_manager = DataFetcherManager()
            return self._data_manager
        except Exception as exc:  # pragma: no cover - fail-open initialization
            self._data_manager_init_error = str(exc)
            return None

    def _build_drawdown(
        self,
        *,
        account_id: Optional[int],
        as_of_date: date,
        cost_method: str,
        threshold_pct: float,
        lookback_days: int,
    ) -> Dict[str, Any]:
        rows = self.repo.list_daily_snapshots_for_risk(
            as_of=as_of_date,
            cost_method=cost_method,
            account_id=account_id,
            lookback_days=lookback_days,
        )
        if not rows:
            return {
                "series_points": 0,
                "max_drawdown_pct": 0.0,
                "current_drawdown_pct": 0.0,
                "alert": False,
                "fx_stale": False,
            }

        grouped: Dict[str, float] = {}
        stale_flag = False
        for row in rows:
            key = row.snapshot_date.isoformat()
            converted, stale, _ = self.portfolio_service.convert_amount(
                amount=float(row.total_equity or 0.0),
                from_currency=str(row.base_currency or "CNY"),
                to_currency="CNY",
                as_of_date=row.snapshot_date,
            )
            grouped[key] = grouped.get(key, 0.0) + converted
            stale_flag = stale_flag or stale or bool(row.fx_stale)

        series: List[Tuple[str, float]] = sorted(grouped.items(), key=lambda item: item[0])
        peak = 0.0
        max_drawdown = 0.0
        current_drawdown = 0.0
        for _, equity in series:
            peak = max(peak, equity)
            if peak <= 0:
                drawdown = 0.0
            else:
                drawdown = (peak - equity) / peak * 100.0
            max_drawdown = max(max_drawdown, drawdown)
            current_drawdown = drawdown

        return {
            "series_points": len(series),
            "max_drawdown_pct": round(max_drawdown, 4),
            "current_drawdown_pct": round(current_drawdown, 4),
            "alert": bool(max_drawdown >= threshold_pct),
            "fx_stale": stale_flag,
        }

    @staticmethod
    def _build_stop_loss(snapshot: Dict[str, Any], thresholds: Dict[str, Any]) -> Dict[str, Any]:
        stop_loss_pct = float(thresholds["stop_loss_alert_pct"])
        near_ratio = float(thresholds["stop_loss_near_ratio"])
        near_threshold = stop_loss_pct * near_ratio

        warnings: List[Dict[str, Any]] = []
        for account in snapshot.get("accounts", []):
            for pos in account.get("positions", []):
                avg_cost = float(pos.get("avg_cost", 0.0) or 0.0)
                last_price = float(pos.get("last_price", 0.0) or 0.0)
                if avg_cost <= 0:
                    continue
                loss_pct = max(0.0, (avg_cost - last_price) / avg_cost * 100.0)
                if loss_pct < near_threshold:
                    continue
                warnings.append(
                    {
                        "account_id": account.get("account_id"),
                        "symbol": pos.get("symbol"),
                        "avg_cost": round(avg_cost, 8),
                        "last_price": round(last_price, 8),
                        "loss_pct": round(loss_pct, 4),
                        "near_threshold_pct": round(near_threshold, 4),
                        "is_triggered": bool(loss_pct >= stop_loss_pct),
                    }
                )

        warnings.sort(key=lambda item: item["loss_pct"], reverse=True)
        return {
            "near_alert": len(warnings) > 0,
            "triggered_count": sum(1 for item in warnings if item["is_triggered"]),
            "near_count": len(warnings),
            "items": warnings[:20],
        }
