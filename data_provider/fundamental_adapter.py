# -*- coding: utf-8 -*-
"""
AkShare fundamental adapter (fail-open).

This adapter intentionally uses capability probing against multiple AkShare
endpoint candidates. It should never raise to caller; partial data is allowed.
"""

from __future__ import annotations

import logging
import re
from datetime import datetime, timedelta
from typing import Any, Dict, List, Optional, Tuple

import pandas as pd
import requests

logger = logging.getLogger(__name__)

_DIVIDEND_KEYWORD_MAP: Dict[str, List[str]] = {
    "per_share": [
        "每股派息",
        "每股现金红利",
        "每股分红",
        "每股派现",
        "派现(元/股)",
        "派息(元/股)",
        "税前派息(元/股)",
        "现金分红(税前)",
    ],
    "plan_text": [
        "分配方案",
        "分红方案",
        "实施方案",
        "派息方案",
        "方案",
        "预案",
        "方案说明",
    ],
    "ex_dividend_date": ["除权除息日", "除息日", "除权日", "除权除息", "除息日期"],
    "record_date": ["股权登记日", "登记日"],
    "announce_date": ["公告日期", "公告日", "实施公告日", "预案公告日"],
    "report_date": ["报告期", "报告日期", "截止日期", "统计截止日期"],
}


def _safe_float(value: Any) -> Optional[float]:
    """Best-effort float conversion."""
    if value is None:
        return None
    if isinstance(value, (int, float)):
        try:
            return float(value)
        except (TypeError, ValueError):
            return None
    s = str(value).strip().replace(",", "").replace("%", "")
    if not s:
        return None
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def _safe_str(value: Any) -> str:
    if value is None:
        return ""
    return str(value).strip()


def _safe_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    try:
        parsed = pd.to_datetime(value)
    except Exception:
        return None
    if pd.isna(parsed):
        return None
    try:
        return parsed.to_pydatetime()
    except Exception:
        return None


def _normalize_code(raw: Any) -> str:
    s = _safe_str(raw).upper()
    if "." in s:
        s = s.split(".", 1)[0]
    s = re.sub(r"^(SH|SZ|BJ)", "", s)
    return s


def _pick_by_keywords(row: pd.Series, keywords: List[str]) -> Optional[Any]:
    """
    Return first non-empty row value whose column name contains any keyword.
    """
    for col in row.index:
        col_s = str(col)
        if any(k in col_s for k in keywords):
            val = row.get(col)
            if val is not None and str(val).strip() not in ("", "-", "nan", "None"):
                return val
    return None


def _parse_dividend_plan_to_per_share(plan_text: str) -> Optional[float]:
    """Parse per-share cash dividend from Chinese plan text."""
    text = _safe_str(plan_text)
    if not text:
        return None

    for pattern in (
        r"(?:每)?\s*10\s*股?\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元",
        r"10\s*派\s*([0-9]+(?:\.[0-9]+)?)\s*元",
    ):
        match = re.search(pattern, text)
        if match:
            parsed = _safe_float(match.group(1))
            if parsed is not None and parsed > 0:
                return parsed / 10.0

    match_per_share = re.search(r"每\s*股\s*派(?:发)?\s*([0-9]+(?:\.[0-9]+)?)\s*元", text)
    if match_per_share:
        parsed = _safe_float(match_per_share.group(1))
        if parsed is not None and parsed > 0:
            return parsed
    return None


def _extract_cash_dividend_per_share(row: pd.Series) -> Optional[float]:
    """Extract pre-tax cash dividend per share from a row."""
    plan_text = _safe_str(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["plan_text"]))
    # Keep pre-tax semantics; skip explicit after-tax plans unless pre-tax marker exists.
    if "税后" in plan_text and "税前" not in plan_text and "含税" not in plan_text:
        return None

    direct = _safe_float(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["per_share"]))
    if direct is not None and direct > 0:
        return direct
    return _parse_dividend_plan_to_per_share(plan_text)


def _filter_rows_by_code(df: pd.DataFrame, stock_code: str) -> pd.DataFrame:
    if df is None or df.empty:
        return pd.DataFrame()
    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "symbol", "ts_code"))]
    if not code_cols:
        return df

    target = _normalize_code(stock_code)
    for col in code_cols:
        try:
            series = df[col].astype(str).map(_normalize_code)
            filtered = df[series == target]
            if not filtered.empty:
                return filtered
        except Exception:
            continue
    return pd.DataFrame()


def _normalize_report_date(value: Any) -> Optional[str]:
    parsed = _safe_datetime(value)
    return parsed.date().isoformat() if parsed else None


def _build_dividend_payload(
    dividend_df: pd.DataFrame,
    stock_code: str,
    max_events: int = 5,
) -> Dict[str, Any]:
    work_df = _filter_rows_by_code(dividend_df, stock_code)
    if work_df.empty:
        return {}

    now_date = datetime.now().date()
    ttm_start_date = now_date - timedelta(days=365)
    dedupe_keys = set()
    events: List[Dict[str, Any]] = []

    for _, row in work_df.iterrows():
        if not isinstance(row, pd.Series):
            continue
        ex_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["ex_dividend_date"]))
        record_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["record_date"]))
        announce_dt = _safe_datetime(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["announce_date"]))
        event_dt = ex_dt or record_dt or announce_dt
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if event_date > now_date:
            continue

        per_share = _extract_cash_dividend_per_share(row)
        if per_share is None or per_share <= 0:
            continue

        dedupe_key = (event_date.isoformat(), round(per_share, 6))
        if dedupe_key in dedupe_keys:
            continue
        dedupe_keys.add(dedupe_key)

        events.append(
            {
                "event_date": event_date.isoformat(),
                "ex_dividend_date": ex_dt.date().isoformat() if ex_dt else None,
                "record_date": record_dt.date().isoformat() if record_dt else None,
                "announcement_date": announce_dt.date().isoformat() if announce_dt else None,
                "cash_dividend_per_share": round(per_share, 6),
                "is_pre_tax": True,
            }
        )

    if not events:
        return {}

    events.sort(key=lambda item: item.get("event_date") or "", reverse=True)
    ttm_events: List[Dict[str, Any]] = []
    for item in events:
        event_dt = _safe_datetime(item.get("event_date"))
        if event_dt is None:
            continue
        event_date = event_dt.date()
        if ttm_start_date <= event_date <= now_date:
            ttm_events.append(item)

    return {
        "events": events[:max(1, max_events)],
        "ttm_event_count": len(ttm_events),
        "ttm_cash_dividend_per_share": (
            round(sum(float(item.get("cash_dividend_per_share") or 0.0) for item in ttm_events), 6)
            if ttm_events else None
        ),
        "coverage": "cash_dividend_pre_tax",
        "as_of": now_date.isoformat(),
    }


def _extract_latest_row(df: pd.DataFrame, stock_code: str) -> Optional[pd.Series]:
    """
    Select the most relevant row for the given stock.
    """
    if df is None or df.empty:
        return None

    code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码", "ts_code", "symbol"))]
    target = _normalize_code(stock_code)
    if code_cols:
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                matched = df[series == target]
                if not matched.empty:
                    return matched.iloc[0]
            except Exception:
                continue
        return None

    # Fallback: use latest row
    return df.iloc[0]


def _em_secucode(stock_code: str) -> str:
    """Convert a bare A-share code into Eastmoney SECUCODE (e.g. 000630 -> 000630.SZ)."""
    code = _normalize_code(stock_code)
    if not code or len(code) != 6:
        return code
    if code.startswith(("6", "9", "5")):
        market = "SH"
    elif code.startswith(("4", "8", "92")):
        market = "BJ"
    else:
        market = "SZ"
    return f"{code}.{market}"


def _fetch_em_datacenter_fundamentals(stock_code: str) -> Dict[str, Any]:
    """Fetch fundamental indicators straight from Eastmoney datacenter.

    Uses two public JSON endpoints (major financial indicators + income
    statement). Fast (~0.3s) and not dependent on AkShare, so it is used as the
    preferred A-share financial source. Returns normalized fields or an empty
    payload with an error note; never raises.
    """
    secucode = _em_secucode(stock_code)
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"}
    payload: Dict[str, Any] = {"report_date": None, "source": "em_datacenter"}

    def _get_rows(report_name: str) -> list:
        url = (
            "https://datacenter-web.eastmoney.com/api/data/v1/get?"
            f"reportName={report_name}&columns=ALL&"
            f"filter=(SECUCODE%3D%22{secucode}%22)&pageSize=1&"
            "sortColumns=REPORT_DATE&sortTypes=-1"
        )
        resp = requests.get(url, headers=headers, timeout=10)
        return (resp.json().get("result") or {}).get("data") or []

    try:
        rows = _get_rows("RPT_F10_FINANCE_MAINFINADATA")
        if rows:
            row = rows[0]
            payload.update(
                {
                    "roe": _safe_float(row.get("ROEJQ")),
                    "roe_yoy": _safe_float(row.get("ROEJQTZ")),
                    "gross_margin": _safe_float(row.get("XSMLL")),
                    "gross_margin_yoy": _safe_float(row.get("XSMLL_TB")),
                    "revenue_yoy": _safe_float(row.get("TOTALOPERATEREVETZ")),
                    "net_profit_yoy": _safe_float(row.get("PARENTNETPROFITTZ")),
                    "eps": _safe_float(row.get("BASIC_EPS")),
                    "bps": _safe_float(row.get("MGZBGJ")),
                    "operating_cash_flow_per_share": _safe_float(row.get("MGJYXJJE")),
                    "deducted_net_profit": _safe_float(row.get("KCFJCXSYJLR")),
                    "report_date": _normalize_report_date(row.get("REPORT_DATE"))
                    or _safe_str(row.get("REPORT_DATE_NAME")),
                }
            )
    except Exception as exc:
        payload["error"] = f"MAINFINADATA:{type(exc).__name__}"

    try:
        rows = _get_rows("RPT_DMSK_FN_INCOME")
        if rows:
            row = rows[0]
            payload.update(
                {
                    "revenue": _safe_float(row.get("TOTAL_OPERATE_INCOME")),
                    "net_profit_parent": _safe_float(row.get("PARENT_NETPROFIT")),
                    "net_margin": _safe_float(row.get("PARENT_NETPROFIT_RATIO")),
                }
            )
            if payload.get("report_date") is None:
                payload["report_date"] = _normalize_report_date(row.get("REPORT_DATE"))
    except Exception as exc:
        payload["error"] = (payload.get("error") or "") + f"; INCOME:{type(exc).__name__}"

    return payload


class AkshareFundamentalAdapter:
    """AkShare adapter for fundamentals, capital flow and dragon-tiger signals."""

    def _call_df_candidates(
        self,
        candidates: List[Tuple[str, Dict[str, Any]]],
    ) -> Tuple[Optional[pd.DataFrame], Optional[str], List[str]]:
        errors: List[str] = []
        try:
            import akshare as ak
        except Exception as exc:
            return None, None, [f"import_akshare:{type(exc).__name__}"]

        for func_name, kwargs in candidates:
            fn = getattr(ak, func_name, None)
            if fn is None:
                continue
            try:
                df = fn(**kwargs)
                if isinstance(df, pd.Series):
                    df = df.to_frame().T
                if isinstance(df, pd.DataFrame) and not df.empty:
                    return df, func_name, errors
            except Exception as exc:
                errors.append(f"{func_name}:{type(exc).__name__}")
                continue
        return None, None, errors

    def get_fundamental_bundle(self, stock_code: str) -> Dict[str, Any]:
        """
        Return normalized fundamental blocks from AkShare with partial tolerance.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "growth": {},
            "earnings": {},
            "institution": {},
            "source_chain": [],
            "errors": [],
        }

        # 东财 datacenter 直连（首选：快且稳，不经 AkShare）
        em_fund = _fetch_em_datacenter_fundamentals(stock_code)
        if any(
            em_fund.get(key) is not None
            for key in (
                "revenue_yoy",
                "net_profit_yoy",
                "roe",
                "gross_margin",
                "revenue",
                "net_profit_parent",
            )
        ):
            result["growth"] = {
                "revenue_yoy": em_fund.get("revenue_yoy"),
                "net_profit_yoy": em_fund.get("net_profit_yoy"),
                "roe": em_fund.get("roe"),
                "gross_margin": em_fund.get("gross_margin"),
            }
            financial_report = {
                "report_date": em_fund.get("report_date"),
                "revenue": em_fund.get("revenue"),
                "net_profit_parent": em_fund.get("net_profit_parent"),
                "operating_cash_flow": em_fund.get("operating_cash_flow_per_share"),
                "roe": em_fund.get("roe"),
                "net_margin": em_fund.get("net_margin"),
            }
            if any(v is not None for v in financial_report.values()):
                result["earnings"]["financial_report"] = financial_report
            result["source_chain"].append("growth:em_datacenter")
            result["status"] = "partial" if result["growth"] or result["earnings"] else result["status"]
            if em_fund.get("error"):
                result["errors"].append(em_fund["error"])

        # Financial indicators (AkShare, kept as a supplement when datacenter is unreachable)
        fin_df, fin_source, fin_errors = self._call_df_candidates([
            ("stock_financial_analysis_indicator", {"symbol": stock_code}),
            ("stock_financial_abstract", {"symbol": stock_code}),
            ("stock_financial_analysis_indicator", {}),
        ])
        result["errors"].extend(fin_errors)
        if fin_df is not None:
            row = _extract_latest_row(fin_df, stock_code)
            if row is not None:
                revenue_yoy = _safe_float(_pick_by_keywords(row, ["营业收入同比", "营收同比", "收入同比", "同比增长"]))
                profit_yoy = _safe_float(_pick_by_keywords(row, ["净利润同比", "净利同比", "归母净利润同比"]))
                roe = _safe_float(_pick_by_keywords(row, ["净资产收益率", "ROE", "净资产收益"]))
                gross_margin = _safe_float(_pick_by_keywords(row, ["毛利率"]))
                report_date = _normalize_report_date(_pick_by_keywords(row, _DIVIDEND_KEYWORD_MAP["report_date"]))
                revenue = _safe_float(_pick_by_keywords(row, ["营业总收入", "营业收入", "营收"]))
                net_profit_parent = _safe_float(_pick_by_keywords(row, ["归母净利润", "母公司股东净利润", "净利润"]))
                operating_cash_flow = _safe_float(
                    _pick_by_keywords(row, ["经营活动产生的现金流量净额", "经营现金流", "经营活动现金流"])
                )
                # 东财 datacenter 已有数据时保留其值，AkShare 仅作补充（避免覆盖更完整的数据）
                if not any(result["growth"].values()):
                    result["growth"] = {
                        "revenue_yoy": revenue_yoy,
                        "net_profit_yoy": profit_yoy,
                        "roe": roe,
                        "gross_margin": gross_margin,
                    }
                financial_report_payload = {
                    "report_date": report_date,
                    "revenue": revenue,
                    "net_profit_parent": net_profit_parent,
                    "operating_cash_flow": operating_cash_flow,
                    "roe": roe,
                }
                existing_report = result["earnings"].get("financial_report") or {}
                # 优先保留 datacenter 的财务报告；缺失字段用 AkShare 补齐
                merged_report = dict(financial_report_payload)
                for key in ("report_date", "revenue", "net_profit_parent", "operating_cash_flow", "roe"):
                    if existing_report.get(key) is not None:
                        merged_report[key] = existing_report[key]
                if any(v is not None for v in merged_report.values()):
                    result["earnings"]["financial_report"] = merged_report
                result["source_chain"].append(f"growth:{fin_source}")

        # Earnings forecast / quick report:
        # AkShare 业绩预告/快报接口新版已不接收 symbol 参数且为全市场慢接口，
        # 会拖垮基本面阶段超时；数据由上方 em_datacenter（净利同比等）覆盖，
        # 故此处不再调用，避免无谓耗时。

        # Dividend details (cash dividend, pre-tax)
        dividend_df, dividend_source, dividend_errors = self._call_df_candidates([
            ("stock_fhps_detail_em", {"symbol": stock_code}),
            ("stock_history_dividend_detail", {"symbol": stock_code, "indicator": "分红", "date": ""}),
            ("stock_dividend_cninfo", {"symbol": stock_code}),
        ])
        result["errors"].extend(dividend_errors)
        if dividend_df is not None:
            dividend_payload = _build_dividend_payload(dividend_df, stock_code, max_events=5)
            if dividend_payload:
                result["earnings"]["dividend"] = dividend_payload
                result["source_chain"].append(f"dividend:{dividend_source}")

        # Institution / top shareholders: 相关 AkShare 全市场接口慢且结构不稳，
        # 在基本面阶段超时预算内收益低，跳过以避免拖垮 growth/earnings 抓取。

        has_content = bool(result["growth"] or result["earnings"] or result["institution"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def _fetch_sina_capital_flow(self, stock_code: str) -> Dict[str, Any]:
        """
        Sina capital-flow fallback used when Eastmoney/AkShare endpoints are
        rate-limited or unreachable. Returns latest main-net-inflow plus 5/10-day
        cumulative net inflow; returns {} if the symbol is not covered.
        """
        code = _normalize_code(stock_code)
        if not code or not code.isdigit():
            return {}
        prefix = "sh" if code.startswith("6") else "sz"
        url = (
            "https://vip.stock.finance.sina.com.cn/quotes_service/api/json_v2.php/"
            f"MoneyFlow.ssl_qsfx_zjlrqs?daima={prefix}{code}"
        )
        try:
            resp = requests.get(
                url,
                headers={
                    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36",
                    "Referer": "https://finance.sina.com.cn/",
                },
                timeout=10,
            )
            resp.raise_for_status()
            payload = resp.json()
        except Exception as exc:  # fail-open
            logger.warning("Sina capital-flow fallback failed for %s: %s", stock_code, exc)
            return {}

        if not isinstance(payload, list) or not payload:
            logger.info("Sina capital-flow: no records for %s", stock_code)
            return {}

        def _net(rec: Any) -> float:
            try:
                val = rec.get("netamount")
                return float(val) if val is not None else 0.0
            except (TypeError, ValueError):
                return 0.0

        records = payload[:10]
        return {
            "main_net_inflow": round(_net(records[0]), 2),
            "inflow_5d": round(sum(_net(r) for r in records[:5]), 2),
            "inflow_10d": round(sum(_net(r) for r in records[:10]), 2),
        }

    def get_capital_flow(self, stock_code: str, top_n: int = 5) -> Dict[str, Any]:
        """
        Return stock + sector capital flow.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "stock_flow": {},
            "sector_rankings": {"top": [], "bottom": []},
            "source_chain": [],
            "errors": [],
        }

        stock_df, stock_source, stock_errors = self._call_df_candidates([
            ("stock_individual_fund_flow", {"stock": stock_code}),
            ("stock_individual_fund_flow", {"symbol": stock_code}),
            ("stock_individual_fund_flow", {}),
            ("stock_main_fund_flow", {"symbol": stock_code}),
            ("stock_main_fund_flow", {}),
        ])
        result["errors"].extend(stock_errors)
        if stock_df is not None:
            row = _extract_latest_row(stock_df, stock_code)
            if row is not None:
                net_inflow = _safe_float(_pick_by_keywords(row, ["主力净流入", "净流入", "净额"]))
                inflow_5d = _safe_float(_pick_by_keywords(row, ["5日", "五日"]))
                inflow_10d = _safe_float(_pick_by_keywords(row, ["10日", "十日"]))
                result["stock_flow"] = {
                    "main_net_inflow": net_inflow,
                    "inflow_5d": inflow_5d,
                    "inflow_10d": inflow_10d,
                }
                result["source_chain"].append(f"capital_stock:{stock_source}")

        # Sina fallback: Eastmoney/AkShare capital-flow endpoints are frequently
        # rate-limited (connection reset), so fall back to Sina when stock flow is
        # still empty after the AkShare candidates.
        if not any(v is not None for v in (result["stock_flow"] or {}).values()):
            sina_flow = self._fetch_sina_capital_flow(stock_code)
            if sina_flow:
                result["stock_flow"] = sina_flow
                result["source_chain"].append("capital_stock:sina")
                result["status"] = "ok"

        sector_df, sector_source, sector_errors = self._call_df_candidates([
            ("stock_sector_fund_flow_rank", {}),
            ("stock_sector_fund_flow_summary", {}),
        ])
        result["errors"].extend(sector_errors)
        if sector_df is not None:
            name_col = next((c for c in sector_df.columns if any(k in str(c) for k in ("板块", "行业", "名称", "name"))), None)
            flow_col = next((c for c in sector_df.columns if any(k in str(c) for k in ("净流入", "主力", "flow", "净额"))), None)
            if name_col and flow_col:
                work_df = sector_df[[name_col, flow_col]].copy()
                work_df[flow_col] = pd.to_numeric(work_df[flow_col], errors="coerce")
                work_df = work_df.dropna(subset=[flow_col])
                top_df = work_df.nlargest(top_n, flow_col)
                bottom_df = work_df.nsmallest(top_n, flow_col)
                result["sector_rankings"] = {
                    "top": [{"name": _safe_str(r[name_col]), "net_inflow": float(r[flow_col])} for _, r in top_df.iterrows()],
                    "bottom": [{"name": _safe_str(r[name_col]), "net_inflow": float(r[flow_col])} for _, r in bottom_df.iterrows()],
                }
                result["source_chain"].append(f"capital_sector:{sector_source}")

        has_content = bool(result["stock_flow"] or result["sector_rankings"]["top"] or result["sector_rankings"]["bottom"])
        result["status"] = "partial" if has_content else "not_supported"
        return result

    def get_dragon_tiger_flag(self, stock_code: str, lookback_days: int = 20) -> Dict[str, Any]:
        """
        Return dragon-tiger signal in lookback window.
        """
        result: Dict[str, Any] = {
            "status": "not_supported",
            "is_on_list": False,
            "recent_count": 0,
            "latest_date": None,
            "source_chain": [],
            "errors": [],
        }

        df, source, errors = self._call_df_candidates([
            ("stock_lhb_stock_statistic_em", {}),
            ("stock_lhb_detail_em", {}),
            ("stock_lhb_jgmmtj_em", {}),
        ])
        result["errors"].extend(errors)
        if df is None:
            return result

        # Try code filter
        code_cols = [c for c in df.columns if any(k in str(c) for k in ("代码", "股票代码", "证券代码"))]
        target = _normalize_code(stock_code)
        matched = pd.DataFrame()
        for col in code_cols:
            try:
                series = df[col].astype(str).map(_normalize_code)
                cur = df[series == target]
                if not cur.empty:
                    matched = cur
                    break
            except Exception:
                continue
        if matched.empty:
            result["source_chain"].append(f"dragon_tiger:{source}")
            result["status"] = "ok" if code_cols else "partial"
            return result

        date_col = next((c for c in matched.columns if any(k in str(c) for k in ("日期", "上榜", "交易日", "time"))), None)
        parsed_dates: List[datetime] = []
        if date_col is not None:
            for val in matched[date_col].astype(str).tolist():
                try:
                    parsed_dates.append(pd.to_datetime(val).to_pydatetime())
                except Exception:
                    continue
        now = datetime.now()
        start = now - timedelta(days=max(1, lookback_days))
        recent_dates = [d for d in parsed_dates if start <= d <= now]

        result["is_on_list"] = bool(recent_dates)
        result["recent_count"] = len(recent_dates) if recent_dates else int(len(matched))
        result["latest_date"] = max(recent_dates).date().isoformat() if recent_dates else (
            max(parsed_dates).date().isoformat() if parsed_dates else None
        )
        result["status"] = "ok"
        result["source_chain"].append(f"dragon_tiger:{source}")
        return result
