# -*- coding: utf-8 -*-
"""Portfolio share card generator.

Generates a 3:4 PNG share card (1080x1440) for a portfolio snapshot, containing:
  - a portfolio stat line (N positions, M sectors)
  - the main-index background and a beat/miss label vs the index (with delta
    when the percent toggle is on)
  - today's P&L (amount / percent / total equity, each independently
    toggleable; disabled values are masked with asterisks instead of hidden)
  - a 7-day equity trend as the P&L card background (portfolio vs SSE dual line,
    normalized to relative change, no amounts)
  - a sector allocation donut chart
  - a compliance disclaimer + generation timestamp watermark

The card intentionally avoids any concrete position details (symbols, cost,
amounts per position) to keep it safe for cross-platform sharing.
"""

from __future__ import annotations

import html as html_mod
import io
import json
import logging
import os
import subprocess
import tempfile
import time
import urllib.request
from datetime import date, datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

logger = logging.getLogger(__name__)

# A-share convention: up = red, down = green
UP_COLOR = "#e04545"
DOWN_COLOR = "#0a8f5c"
FLAT_COLOR = "#7a8599"
INDEX_LINE = "#7c8aa0"
ACCENT = "#2563eb"
BG_TOP = "#f4f8ff"
BG_BOTTOM = "#ffffff"
TEXT_MAIN = "#1c2430"
TEXT_SUB = "#6b7688"
CARD_BG = "#ffffff"
CARD_SHADOW = "0 6px 24px rgba(37,99,235,.08)"

# Fixed 3:4 canvas
CARD_WIDTH = 1080
CARD_HEIGHT = 1440

_SECTOR_PALETTE = [
    "#2563eb", "#0e9f6e", "#d97706", "#7c3aed", "#db2777",
    "#0891b2", "#4f46e5", "#ea580c", "#16a34a", "#9333ea",
    "#0d9488", "#ca8a04", "#dc2626", "#64748b",
]


def _resolve_browser_command() -> Optional[str]:
    """Resolve a system-installed Chromium-family browser (Edge/Chrome)."""
    candidates = [
        r"C:\Program Files (x86)\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Microsoft\Edge\Application\msedge.exe",
        r"C:\Program Files\Google\Chrome\Application\chrome.exe",
        r"C:\Program Files (x86)\Google\Chrome\Application\chrome.exe",
    ]
    for candidate in candidates:
        if os.path.isfile(candidate):
            return candidate
    return None


def _normalize_canvas(image_bytes: bytes, *, target_w: int, target_h: int) -> bytes:
    """Crop or pad the screenshot to the exact target canvas (3:4)."""
    try:
        from PIL import Image

        image = Image.open(io.BytesIO(image_bytes)).convert("RGB")
        w, h = image.size

        scale = target_w / float(w)
        if abs(scale - 1.0) > 0.01:
            image = image.resize((target_w, int(round(h * scale))), Image.LANCZOS)
            w, h = image.size

        if h >= target_h:
            image = image.crop((0, 0, target_w, target_h))
        else:
            canvas = Image.new("RGB", (target_w, target_h), (244, 248, 255))
            canvas.paste(image, (0, 0))
            image = canvas

        output = io.BytesIO()
        image.save(output, format="PNG")
        return output.getvalue()
    except Exception:
        return image_bytes


def _html_to_png(html_text: str, *, width: int = CARD_WIDTH, height: int = CARD_HEIGHT) -> Optional[bytes]:
    """Render a self-contained HTML document to PNG via system Edge headless."""
    browser = _resolve_browser_command()
    if browser is None:
        logger.warning("Edge/Chrome not found; portfolio share image unavailable")
        return None

    temp_dir = tempfile.mkdtemp()
    html_path = Path(temp_dir) / "card.html"
    png_path = Path(temp_dir) / "card.png"
    try:
        html_path.write_text(html_text, encoding="utf-8")
        try:
            subprocess.run(
                [
                    browser,
                    "--headless=new",
                    "--disable-gpu",
                    "--no-first-run",
                    "--hide-scrollbars",
                    f"--window-size={width},{height}",
                    "--force-device-scale-factor=2",
                    f"--screenshot={png_path}",
                    html_path.resolve().as_uri(),
                ],
                capture_output=True,
                timeout=90,
                check=False,
            )
        except Exception as exc:
            logger.error("Edge screenshot error: %s", exc)
            return None
        if not png_path.exists() or png_path.stat().st_size == 0:
            logger.warning("Edge screenshot produced no output")
            return None
        return _normalize_canvas(png_path.read_bytes(), target_w=width * 2, target_h=height * 2)
    finally:
        try:
            import shutil

            shutil.rmtree(temp_dir, ignore_errors=True)
        except Exception:
            pass


def _fmt_amount(value: float) -> str:
    sign = "+" if value > 0 else ("-" if value < 0 else "")
    return f"{sign}{abs(value):,.2f}"


def _fmt_pct(value: float) -> str:
    sign = "+" if value > 0 else ("-" if value < 0 else "")
    return f"{sign}{abs(value):.2f}%"


def _esc(value: Any) -> str:
    return html_mod.escape(str(value if value is not None else ""))


def build_donut_svg(sectors: List[Dict[str, Any]], *, size: int = 380, stroke: int = 56) -> str:
    """Build an SVG donut chart for sector allocation."""
    if not sectors:
        return ""

    radius = (size - stroke) / 2.0
    cx = cy = size / 2.0
    circumference = 2.0 * 3.14159265 * radius
    total = sum(float(s.get("weight_pct") or 0.0) for s in sectors)
    if total <= 0:
        return ""

    start_angle = -90.0
    segments: List[str] = []
    for idx, sec in enumerate(sectors):
        weight = float(sec.get("weight_pct") or 0.0)
        if weight <= 0:
            continue
        frac = weight / total
        seg_len = frac * circumference
        dash_offset = -start_angle / 360.0 * circumference
        color = _SECTOR_PALETTE[idx % len(_SECTOR_PALETTE)]
        segments.append(
            f'<circle cx="50%" cy="50%" r="{radius}" fill="none" stroke="{color}" '
            f'stroke-width="{stroke}" stroke-dasharray="{seg_len:.2f} {circumference - seg_len:.2f}" '
            f'stroke-dashoffset="{dash_offset:.2f}" />'
        )
        start_angle += frac * 360.0

    return (
        f'<svg viewBox="0 0 {size} {size}" width="{size}" height="{size}" '
        f'xmlns="http://www.w3.org/2000/svg" role="img">'
        f'<circle cx="{cx}" cy="{cy}" r="{radius}" fill="none" stroke="#eef2f9" '
        f'stroke-width="{stroke}" />'
        + "".join(segments)
        + "</svg>"
    )


def build_dual_line_svg(
    portfolio: List[float],
    index: Optional[List[float]],
    labels: Optional[List[str]] = None,
    *,
    width: int = 940,
    height: int = 320,
    background: bool = False,
) -> str:
    """Build a dual-line SVG chart (portfolio vs SSE index), normalized to
    relative change with the first day = 100. No absolute values are labelled.

    When ``background`` is True the chart is meant to sit behind a frosted
    panel, so lines/fills use lower opacity.
    """
    if len(portfolio) < 2:
        return ""

    def norm(series: List[float]) -> List[float]:
        if not series:
            return []
        base = series[0] or 1.0
        return [(v / base) * 100.0 for v in series]

    port = norm(portfolio)
    idx = norm(index) if index and len(index) == len(portfolio) else None

    all_vals = list(port) + (list(idx) if idx else [])
    min_v, max_v = min(all_vals), max(all_vals)
    span = (max_v - min_v) or 1.0
    pad_x, pad_y = 14, 20
    inner_w = width - pad_x * 2
    inner_h = height - pad_y * 2

    def px(i: int) -> float:
        return pad_x + inner_w * i / (len(port) - 1)

    def py(v: float) -> float:
        return pad_y + inner_h * (1.0 - (v - min_v) / span)

    port_rising = port[-1] >= port[0]
    port_color = UP_COLOR if port_rising else DOWN_COLOR

    def polyline(series: List[float], color: str, stroke_w: float, opacity: float) -> str:
        coords = " ".join(f"{px(i):.1f},{py(v):.1f}" for i, v in enumerate(series))
        area = (
            f"M {px(0):.1f},{py(series[0]):.1f} "
            + " ".join(f"L {px(i):.1f},{py(v):.1f}" for i, v in enumerate(series))
            + f" L {px(len(series)-1):.1f},{height} L {px(0):.1f},{height} Z"
        )
        return (
            f'<path d="{area}" fill="{color}" opacity="{opacity * 0.18:.2f}" />'
            f'<polyline points="{coords}" fill="none" stroke="{color}" '
            f'stroke-width="{stroke_w}" opacity="{opacity}" '
            f'stroke-linecap="round" stroke-linejoin="round" />'
        )

    parts = [polyline(port, port_color, 6 if not background else 7, 1.0 if not background else 0.55)]
    if idx:
        parts.append(polyline(idx, INDEX_LINE, 4, 1.0 if not background else 0.5))

    # end dots
    parts.append(
        f'<circle cx="{px(len(port)-1):.1f}" cy="{py(port[-1]):.1f}" r="7" '
        f'fill="{port_color}" opacity="{1.0 if not background else 0.7}" '
        f'stroke="#fff" stroke-width="3" />'
    )
    if idx:
        parts.append(
            f'<circle cx="{px(len(idx)-1):.1f}" cy="{py(idx[-1]):.1f}" r="6" '
            f'fill="{INDEX_LINE}" opacity="{1.0 if not background else 0.7}" '
            f'stroke="#fff" stroke-width="3" />'
        )

    # x-axis date labels (bottom)
    label_svg = ""
    if labels:
        pieces = []
        for i, lab in enumerate(labels):
            pieces.append(
                f'<text x="{px(i):.1f}" y="{height - 6:.0f}" font-size="19" '
                f'fill="#9aa4b5" text-anchor="middle" opacity="{1.0 if not background else 0.8}">'
                f"{_esc(lab)}</text>"
            )
        label_svg = "".join(pieces)

    # legend (top-right)
    legend = (
        f'<g opacity="{1.0 if not background else 0.85}">'
        f'<circle cx="{width - 118:.0f}" cy="14" r="7" fill="{port_color}" />'
        f'<text x="{width - 104:.0f}" y="19" font-size="19" fill="{TEXT_SUB}">组合</text>'
    )
    if idx:
        legend += (
            f'<circle cx="{width - 54:.0f}" cy="14" r="7" fill="{INDEX_LINE}" />'
            f'<text x="{width - 40:.0f}" y="19" font-size="19" fill="{TEXT_SUB}">上证</text>'
        )
    legend += "</g>"

    return (
        f'<svg viewBox="0 0 {width} {height}" preserveAspectRatio="xMidYMid meet" '
        f'width="100%" height="100%" '
        f'xmlns="http://www.w3.org/2000/svg" role="img">'
        + "".join(parts)
        + label_svg
        + legend
        + "</svg>"
    )


def compute_day_pnl(snapshot: Dict[str, Any]) -> Dict[str, float]:
    """Compute portfolio today's P&L.

    Primary source: Tencent realtime quotes — (current - prev_close) * qty —
    which works even when the snapshot itself only has historical close prices
    (i.e. when ``day_change_pct`` is missing). Falls back to the snapshot's
    ``day_change_pct`` for symbols the realtime quote cannot resolve.
    """
    positions: List[Dict[str, Any]] = []
    total_cash = 0.0
    for account in snapshot.get("accounts", []):
        total_cash += float(account.get("cash_balance") or account.get("cash") or 0.0)
        positions.extend(account.get("positions", []) or [])

    symbols = [str(p.get("symbol")) for p in positions if float(p.get("quantity") or 0.0) > 0]
    quotes = _fetch_tencent_realtime_quotes(symbols)

    total_prev = 0.0
    total_day_pnl = 0.0
    for pos in positions:
        qty = float(pos.get("quantity") or 0.0)
        if qty <= 0:
            continue
        symbol = str(pos.get("symbol"))
        mv = float(pos.get("market_value_base") or 0.0)
        quote = quotes.get(symbol)
        if quote and quote.get("prev_close", 0.0) > 0 and quote.get("current", 0.0) > 0:
            prev = quote["prev_close"]
            total_prev += prev * qty
            total_day_pnl += (quote["current"] - prev) * qty
            continue
        # Fallback 1: snapshot realtime day change (only present for realtime quotes)
        change_pct = pos.get("day_change_pct")
        if change_pct is not None:
            try:
                change_pct = float(change_pct)
            except (TypeError, ValueError):
                change_pct = None
            if change_pct is not None:
                prev = mv / (1.0 + change_pct / 100.0) if (1.0 + change_pct / 100.0) != 0 else mv
                total_prev += prev
                total_day_pnl += mv - prev
                continue
        # Fallback 2: Tencent daily kline (last two closes) — resilient when the
        # realtime endpoint is temporarily unreachable, so today's P&L never degrades to 0.
        try:
            kline = _fetch_tencent_kline(symbol, count=3)
            closes = sorted((kline or {}).items())[-2:]
            if len(closes) == 2:
                p0 = float(closes[0][1])
                p1 = float(closes[1][1])
                if p0 > 0:
                    total_prev += p0 * qty
                    total_day_pnl += (p1 - p0) * qty
                    continue
        except Exception as exc:  # pragma: no cover
            logger.warning("腾讯日K兜底失败 %s: %s", symbol, exc)
        # No usable quote for this symbol — skip rather than fabricate a value.
        continue

    day_pnl_pct = (total_day_pnl / total_prev * 100.0) if total_prev else 0.0
    return {
        "day_pnl_amount": total_day_pnl,
        "day_pnl_pct": day_pnl_pct,
        "prev_total_equity": total_prev + total_cash,
    }


# Short in-memory cache for Tencent daily K-lines (avoid refetch on repeat renders)
_TENCENT_KLINE_CACHE: Dict[str, Dict[str, float]] = {}
_TENCENT_KLINE_TS: Dict[str, float] = {}
_TENCENT_KLINE_TTL = 300.0  # 5 minutes


def _tencent_symbol(symbol: str) -> str:
    s = str(symbol).strip().lower()
    if s.startswith(("sh", "sz")):
        return s
    # A股沪市 60/68；沪市 ETF/LOF 5 开头；沪 B 股 900 开头
    if s.startswith(("60", "68", "5", "900")):
        return "sh" + s
    # 其余（00/30/15/16 等）归深市
    return "sz" + s


# Short in-memory cache for Tencent realtime quotes (avoid refetch on repeat renders)
_TENCENT_QUOTE_CACHE: Dict[str, Tuple[float, Optional[Dict[str, Any]]]] = {}
_TENCENT_QUOTE_TTL = 30.0  # 30 seconds


def _fetch_tencent_realtime_quotes(symbols: List[str]) -> Dict[str, Dict[str, Any]]:
    """Fetch current price / previous close for a batch of symbols from Tencent's
    free realtime quote endpoint (qt.gtimg.cn).

    Returns {symbol: {"current": float, "prev_close": float, "change_pct": float}}.
    Only successfully-resolved symbols are included; the dict is empty on total failure.
    """
    unique = list(dict.fromkeys(s for s in (symbols or []) if s))
    if not unique:
        return {}
    now = time.time()
    out: Dict[str, Dict[str, Any]] = {}
    missing: List[str] = []
    for s in unique:
        cached = _TENCENT_QUOTE_CACHE.get(s)
        if cached is not None and (now - cached[0]) < _TENCENT_QUOTE_TTL:
            if cached[1] is not None:
                out[s] = cached[1]
        else:
            missing.append(s)
    if not missing:
        return out

    tsyms = [_tencent_symbol(s) for s in missing]
    url = "https://qt.gtimg.cn/q=" + ",".join(tsyms)
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("gbk", errors="ignore")
    except Exception as exc:  # pragma: no cover
        logger.warning("腾讯实时行情获取失败: %s", exc)
        return out

    for line in raw.split(";"):
        if "=" not in line:
            continue
        key, val = line.split("=", 1)
        sym = str(key).strip()
        if sym.startswith("v_"):
            sym = sym[2:]
        body = val.strip().strip('"').strip()
        fields = body.split("~")
        if len(fields) < 33 or not fields[3]:
            continue
        try:
            current = float(fields[3])
            prev_close = float(fields[4]) if fields[4] else 0.0
            change_pct = float(fields[32]) if fields[32] else 0.0
        except (TypeError, ValueError):
            continue
        if current <= 0:
            continue
        for s in missing:
            if _tencent_symbol(s) == sym:
                quote = {"current": current, "prev_close": prev_close, "change_pct": change_pct}
                out[s] = quote
                _TENCENT_QUOTE_CACHE[s] = (now, quote)
                break
    return out


def _fetch_tencent_kline(symbol: str, count: int = 12) -> Dict[str, float]:
    """Fetch recent daily closes from Tencent's free fqkline API.

    Returns {YYYY-MM-DD: close}. Uses a 5-minute in-memory cache.
    """
    tsym = _tencent_symbol(symbol)
    now = time.time()
    cached = _TENCENT_KLINE_CACHE.get(tsym)
    cached_ts = _TENCENT_KLINE_TS.get(tsym, 0.0)
    if cached is not None and (now - cached_ts) < _TENCENT_KLINE_TTL:
        return cached

    url = (
        "https://web.ifzq.gtimg.cn/appstock/app/fqkline/get"
        f"?param={tsym},day,,,{count},qfq"
    )
    try:
        req = urllib.request.Request(
            url,
            headers={"User-Agent": "Mozilla/5.0", "Referer": "https://gu.qq.com/"},
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            payload = json.loads(resp.read().decode("utf-8"))
        node = (payload.get("data") or {}).get(tsym) or {}
        rows = node.get("qfqday") or node.get("day") or []
        out: Dict[str, float] = {}
        for row in rows:
            if len(row) >= 3 and row[0] and row[2]:
                try:
                    out[str(row[0])] = float(row[2])
                except (TypeError, ValueError):
                    continue
        if out:
            _TENCENT_KLINE_CACHE[tsym] = out
            _TENCENT_KLINE_TS[tsym] = now
        return out
    except Exception as exc:  # pragma: no cover
        logger.warning("腾讯K线获取失败 %s: %s", tsym, exc)
        return {}


def build_sparkline_payload(snapshot: Dict[str, Any], *, days: int = 7) -> Dict[str, Any]:
    """Build 7-day trend data: portfolio total equity and SSE index closes.

    Returns:
      {
        "labels": ["08/19", ...],
        "portfolio": [equity, ...],
        "index": [close, ...],   # aligned to portfolio dates (may be empty)
      }
    """
    positions: List[Tuple[str, float]] = []
    total_cash = 0.0
    for account in snapshot.get("accounts", []):
        total_cash += float(account.get("cash_balance") or account.get("cash") or 0.0)
        for pos in account.get("positions", []) or []:
            qty = float(pos.get("quantity") or 0.0)
            if qty > 0:
                positions.append((str(pos.get("symbol")), qty))

    empty = {"labels": [], "portfolio": [], "index": []}
    if not positions:
        return empty

    daily_close: Dict[str, Dict[str, float]] = {}
    for symbol, _qty in positions:
        closes = _fetch_tencent_kline(symbol, count=days + 4)
        if closes:
            daily_close[symbol] = closes
    if not daily_close:
        return empty

    all_dates = sorted({d for closes in daily_close.values() for d in closes})
    if not all_dates:
        return empty
    all_dates = all_dates[-days:]

    portfolio_series: List[float] = []
    for day_str in all_dates:
        total = total_cash
        for symbol, qty in positions:
            closes = daily_close.get(symbol)
            if not closes:
                continue
            price = closes.get(day_str)
            if price is None:
                prev = [closes[k] for k in sorted(closes) if k <= day_str]
                price = prev[-1] if prev else None
            if price is not None:
                total += price * qty
        portfolio_series.append(total)

    # SSE index series aligned to the same dates
    index_closes = _fetch_tencent_kline("sh000001", count=days + 4)
    index_series: List[float] = []
    if index_closes:
        sorted_dates = sorted(index_closes)
        last = None
        for day_str in all_dates:
            if day_str in index_closes:
                last = index_closes[day_str]
            index_series.append(last if last is not None else 0.0)

    labels = [d[5:].replace("-", "/") for d in all_dates]
    return {
        "labels": labels,
        "portfolio": portfolio_series,
        "index": index_series,
    }


def fetch_main_index() -> Optional[Dict[str, Any]]:
    """Fetch the Shanghai Composite Index today change.

    Primary source: Tencent realtime quote (reliable on this machine).
    Fallback: DataFetcherManager.get_main_indices.
    """
    try:
        req = urllib.request.Request(
            "https://qt.gtimg.cn/q=sh000001",
            headers={"User-Agent": "Mozilla/5.0"},
        )
        with urllib.request.urlopen(req, timeout=8) as resp:
            raw = resp.read().decode("gbk", errors="ignore")
        if "=" in raw:
            body = raw.split("=", 1)[1].strip().strip('"').strip(";")
            fields = body.split("~")
            # Tencent index quote layout: [1]=name [2]=code [3]=current [4]=prev_close
            # [30]=timestamp [31]=change [32]=change_pct [33]=high [34]=low
            if len(fields) > 33 and fields[3]:
                current = float(fields[3])
                prev_close = float(fields[4]) if fields[4] else 0.0
                change_pct = float(fields[32]) if fields[32] else 0.0
                if not change_pct and prev_close:
                    change = float(fields[31]) if fields[31] else 0.0
                    change_pct = change / prev_close * 100.0 if prev_close else 0.0
                return {
                    "code": "sh000001",
                    "name": fields[1] or "上证指数",
                    "current": current,
                    "change_pct": change_pct,
                }
    except Exception as exc:  # pragma: no cover
        logger.warning("腾讯指数获取失败: %s", exc)

    try:
        from data_provider.base import DataFetcherManager

        indices = DataFetcherManager().get_main_indices(region="cn") or []
        for idx in indices:
            if str(idx.get("code", "")).lower() in {"sh000001", "000001.sh"}:
                return idx
        if indices:
            return indices[0]
    except Exception as exc:  # pragma: no cover
        logger.warning("获取大盘指数失败: %s", exc)
    return None


def _count_positions(snapshot: Dict[str, Any]) -> int:
    symbols = set()
    for account in snapshot.get("accounts", []):
        for pos in account.get("positions", []) or []:
            sym = str(pos.get("symbol") or "").strip()
            if sym and float(pos.get("quantity") or 0.0) > 0:
                symbols.add(sym)
    return len(symbols)


def build_card_html(
    *,
    card_date: str,
    day_pnl_amount: float,
    day_pnl_pct: float,
    total_equity: float,
    sectors: List[Dict[str, Any]],
    position_count: int = 0,
    trend_svg: str = "",
    trend_pct: float = 0.0,
    position_ratio: float = 0.0,
    main_index: Optional[Dict[str, Any]] = None,
    generated_at: Optional[str] = None,
    show_pnl_amount: bool = True,
    show_pnl_pct: bool = True,
    show_equity: bool = True,
) -> str:
    """Build the share-card HTML document (fixed 3:4 canvas)."""
    # Guard: never allow all three toggles off; fall back to showing the amount.
    if not (show_pnl_amount or show_pnl_pct or show_equity):
        show_pnl_amount = True

    if day_pnl_amount >= 0:
        pnl_color = UP_COLOR
        pnl_label = "今日盈利"
        direction_text = "上涨" if day_pnl_pct > 0 else "持平"
    else:
        pnl_color = DOWN_COLOR
        pnl_label = "今日亏损"
        direction_text = "下跌"

    total_equity_text = f"¥{total_equity:,.2f}" if total_equity else "—"
    mask_color = "#aab4c6"

    # ---- Primary number: the first enabled toggle by priority (amount > pct > equity) ----
    primary = None
    if show_pnl_amount:
        primary = "amount"
    elif show_pnl_pct:
        primary = "pct"
    elif show_equity:
        primary = "equity"

    rows: List[str] = []
    order = ["amount", "pct", "equity"]

    def push_one(toggle: str, big: bool) -> None:
        if toggle == "amount":
            if show_pnl_amount:
                if big:
                    rows.append(
                        f'<div class="pnl-primary" style="color:{pnl_color}">{_fmt_amount(day_pnl_amount)}</div>'
                    )
                else:
                    rows.append(
                        f'<div class="pnl-secondary" style="color:{pnl_color}">{_fmt_amount(day_pnl_amount)}</div>'
                    )
            else:
                rows.append(f'<div class="pnl-mask" style="color:{mask_color}">****</div>')
        elif toggle == "pct":
            if show_pnl_pct:
                if big:
                    rows.append(
                        f'<div class="pnl-primary" style="color:{pnl_color}">{_fmt_pct(day_pnl_pct)}</div>'
                    )
                else:
                    rows.append(
                        f'<div class="pnl-secondary" style="color:{pnl_color}">{_fmt_pct(day_pnl_pct)}</div>'
                    )
            else:
                rows.append(f'<div class="pnl-mask" style="color:{mask_color}">****</div>')
        elif toggle == "equity":
            if show_equity:
                if big:
                    rows.append(
                        f'<div class="pnl-primary" style="color:{TEXT_MAIN}">{total_equity_text}</div>'
                    )
                else:
                    rows.append(
                        f'<div class="pnl-equity-line">总资产 <b>{total_equity_text}</b></div>'
                    )
            else:
                rows.append(
                    f'<div class="pnl-mask" style="color:{mask_color}">总资产 ¥****</div>'
                )

    # Primary (C position) always comes first; the rest follow in fixed priority.
    if primary:
        push_one(primary, big=True)
    for toggle in order:
        if toggle != primary:
            push_one(toggle, big=False)

    if primary in ("amount", "pct"):
        caption = f'<div class="pnl-caption">{pnl_label} · 今日{direction_text}</div>'
    elif primary == "equity":
        caption = f'<div class="pnl-caption">账户总资产</div>'
    else:
        caption = ""

    trend_legend = ""
    if trend_svg:
        trend_legend = (
            '<div class="trend-legend">近7日走势（组合 vs 上证 · 相对涨幅）</div>'
        )

    pnl_block = f"""
    <div class="section-title">今日盈亏</div>
    <div class="pnl-box">
      {trend_svg}
      <div class="pnl-overlay">
        <div class="pnl-numbers">
          {''.join(rows)}
          {caption}
        </div>
        {trend_legend}
      </div>
    </div>"""

    # ---- KPI strip: 7-day return + position ratio ----
    trend_color = UP_COLOR if trend_pct >= 0 else DOWN_COLOR
    kpi_items = []
    if trend_svg:
        kpi_items.append(
            f'<span class="kpi-item">近7日 <b class="kpi-val" style="color:{trend_color}">{_fmt_pct(trend_pct)}</b></span>'
        )
    if position_ratio > 0:
        kpi_items.append(
            f'<span class="kpi-item">仓位 <b class="kpi-val" style="color:{TEXT_MAIN}">{position_ratio:.2f}%</b></span>'
        )
    kpi_strip = (
        f'<div class="kpi-strip">{"".join(f"<span class=\"kpi-sep\">｜</span>{item}" if i else item for i, item in enumerate(kpi_items))}</div>'
        if kpi_items
        else ""
    )

    # ---- Main index + beat/miss line (always shown; delta only when pct on) ----
    index_line = ""
    if main_index:
        idx_name = _esc(main_index.get("name") or "上证指数")
        idx_pct = float(main_index.get("change_pct") or 0.0)
        idx_color = UP_COLOR if idx_pct > 0 else (DOWN_COLOR if idx_pct < 0 else FLAT_COLOR)
        diff = day_pnl_pct - idx_pct
        if abs(diff) < 0.005:
            beat_text = "组合与大盘基本持平"
        elif diff > 0:
            beat_text = "组合今日跑赢大盘" + (f" {abs(diff):.2f}%" if show_pnl_pct else "")
        else:
            beat_text = "组合今日跑输大盘" + (f" {abs(diff):.2f}%" if show_pnl_pct else "")
        beat_color = UP_COLOR if diff >= 0 else DOWN_COLOR
        index_line = (
            f'<div class="index-line">'
            f'<span class="index-name">{idx_name}</span> '
            f'<span style="color:{idx_color}">{_fmt_pct(idx_pct)}</span>'
            f'<span class="beat-tag" style="color:{beat_color}">· {_esc(beat_text)}</span>'
            f'</div>'
        )

    # ---- Portfolio stat line ----
    sector_count = len([s for s in sectors if float(s.get("weight_pct") or 0.0) > 0])
    stats_items = []
    if position_count > 0:
        stats_items.append(f"持有 {position_count} 只")
    if sector_count > 0:
        stats_items.append(f"覆盖 {sector_count} 个行业")
    stats_line = (
        f'<div class="stats-line">{"&nbsp;·&nbsp;".join(_esc(x) for x in stats_items)}</div>'
        if stats_items else ""
    )

    # ---- Sector donut ----
    if sectors:
        top1 = sectors[0]
        top1_name = _esc(str(top1.get("sector") or "—"))
        top1_weight = float(top1.get("weight_pct") or 0.0)
        donut_center = (
            f'<div class="donut-center"><div class="donut-center-name">{top1_name}</div>'
            f'<div class="donut-center-weight">{top1_weight:.2f}%</div>'
            f'<div class="donut-center-caption">第一大行业</div></div>'
        )
        legend_rows = []
        for idx, sec in enumerate(sectors):
            color = _SECTOR_PALETTE[idx % len(_SECTOR_PALETTE)]
            weight = float(sec.get("weight_pct") or 0.0)
            legend_rows.append(
                f'<div class="legend-row">'
                f'<span class="legend-dot" style="background:{color}"></span>'
                f'<span class="legend-name">{_esc(sec.get("sector") or "其他")}</span>'
                f'<span class="legend-weight">{weight:.2f}%</span>'
                f'</div>'
            )
        donut_svg = build_donut_svg(sectors)
        sector_block = f"""
    <div class="section-title">投资行业占比</div>
    <div class="sector-box">
      <div class="donut-wrap">
        {donut_svg}
        {donut_center}
      </div>
      <div class="legend">{''.join(legend_rows)}</div>
    </div>"""
    else:
        sector_block = f"""
    <div class="section-title">投资行业占比</div>
    <div class="sector-box"><div class="empty-hint">暂无行业数据</div></div>"""

    return f"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8" />
<style>
  * {{ margin:0; padding:0; box-sizing:border-box; }}
  html, body {{ width:1080px; min-height:1440px; }}
  body {{ font-family:'PingFang SC','Microsoft YaHei','Noto Sans SC',sans-serif;
         background:linear-gradient(180deg,{BG_TOP} 0%,{BG_BOTTOM} 100%);
         color:{TEXT_MAIN}; }}
  .card {{ width:1080px; min-height:1440px; padding:52px 64px 40px; display:flex; flex-direction:column; }}
  .header {{ display:flex; align-items:flex-end; justify-content:space-between; }}
  .brand {{ font-size:30px; font-weight:800; letter-spacing:1px; }}
  .brand .dot {{ color:{ACCENT}; }}
  .date {{ margin-top:9px; font-size:20px; color:{TEXT_SUB}; }}
  .badge {{ background:{ACCENT}; color:#fff; font-size:20px; font-weight:600; padding:7px 20px; border-radius:999px; }}
  .stats-line {{ margin-top:14px; font-size:26px; font-weight:700; color:{TEXT_MAIN};
                letter-spacing:.5px; }}
  .index-line {{ margin-top:16px; font-size:22px; color:{TEXT_SUB}; }}
  .index-name {{ font-weight:700; color:{TEXT_MAIN}; }}
  .beat-tag {{ font-weight:700; }}
  .section-title {{ font-size:26px; font-weight:800; margin:30px 0 16px; color:{TEXT_MAIN};
                    letter-spacing:.5px; }}
  .pnl-box {{ position:relative; background:{CARD_BG}; border-radius:22px; overflow:hidden;
              box-shadow:{CARD_SHADOW}; }}
  .pnl-box svg {{ position:absolute; inset:0; width:100%; height:100%;
                  display:block; }}
  .pnl-overlay {{ position:relative; padding:34px 40px 26px;
                  background:linear-gradient(180deg, rgba(255,255,255,.93) 0%,
                             rgba(255,255,255,.66) 55%, rgba(255,255,255,.34) 100%); }}
  .pnl-numbers {{ text-align:center; }}
  .pnl-primary {{ font-size:84px; font-weight:800; line-height:1.06; letter-spacing:.5px;
                  font-variant-numeric:tabular-nums; }}
  .pnl-secondary {{ font-size:44px; font-weight:700; margin-top:10px;
                    font-variant-numeric:tabular-nums; }}
  .pnl-equity-line {{ margin-top:12px; font-size:22px; color:{TEXT_SUB}; }}
  .pnl-equity-line b {{ color:{TEXT_MAIN}; font-size:27px; }}
  .pnl-mask {{ font-size:42px; font-weight:700; margin-top:10px; color:{TEXT_SUB};
               letter-spacing:4px; font-variant-numeric:normal; }}
  .pnl-caption {{ margin-top:10px; font-size:21px; color:{TEXT_SUB}; }}
  .trend-legend {{ margin-top:10px; text-align:center; font-size:17px; color:#9aa4b5; }}
  .kpi-strip {{ margin-top:18px; background:{CARD_BG}; border-radius:18px; padding:20px 36px;
                box-shadow:{CARD_SHADOW}; display:flex; align-items:center; justify-content:center;
                gap:28px; }}
  .kpi-item {{ font-size:25px; color:{TEXT_SUB}; font-weight:600; }}
  .kpi-val {{ font-size:29px; font-weight:800; font-variant-numeric:tabular-nums; }}
  .kpi-sep {{ font-size:22px; color:#c7d0de; }}
  .sector-box {{ background:{CARD_BG}; border-radius:22px; padding:30px 36px;
                 box-shadow:{CARD_SHADOW}; display:flex; align-items:center; gap:36px; }}
  .donut-wrap {{ position:relative; width:360px; height:360px; flex-shrink:0; }}
  .donut-wrap svg {{ width:360px; height:360px; }}
  .donut-center {{ position:absolute; inset:0; display:flex; flex-direction:column;
                    align-items:center; justify-content:center; text-align:center; }}
  .donut-center-name {{ font-size:28px; font-weight:800; max-width:200px; }}
  .donut-center-weight {{ font-size:38px; font-weight:800; color:{ACCENT}; margin-top:4px; }}
  .donut-center-caption {{ font-size:18px; color:{TEXT_SUB}; margin-top:4px; }}
  .legend {{ flex:1; display:flex; flex-direction:column; gap:16px; min-width:0; }}
  .legend-row {{ display:flex; align-items:center; gap:12px; }}
  .legend-dot {{ width:20px; height:20px; border-radius:6px; flex-shrink:0; }}
  .legend-name {{ flex:1; font-size:23px; font-weight:600; }}
  .legend-weight {{ width:118px; text-align:right; font-size:23px; font-weight:700;
                     font-variant-numeric:tabular-nums; }}
  .empty-hint {{ font-size:23px; color:{TEXT_SUB}; text-align:center; width:100%; }}
  .footer {{ margin-top:auto; border-top:1px solid #e5ebf5; padding-top:16px;
             display:flex; flex-direction:column; gap:8px; }}
  .motto {{ text-align:center; font-size:22px; font-weight:600; color:#55667f; letter-spacing:1px; }}
  .disclaimer {{ text-align:center; font-size:19px; color:{TEXT_SUB}; line-height:1.55; }}
  .disclaimer b {{ color:{TEXT_MAIN}; }}
  .watermark {{ margin-top:2px; text-align:right; font-size:16px; color:#b6c0d0; }}
</style>
</head>
<body>
  <div class="card">
    <div class="header">
      <div>
        <div class="brand">今日持仓概览<span class="dot"> · </span>个人投资记录</div>
        <div class="date">{_esc(card_date)}</div>
      </div>
      <div class="badge">日报</div>
    </div>

    {stats_line}
    {index_line}
    {pnl_block}
    {kpi_strip}
    {sector_block}

    <div class="footer">
      <div class="motto">做时间的朋友，做确定性的信徒</div>
      <div class="disclaimer"><b>免责声明</b>：以上仅为个人投资记录分享，不构成任何投资建议。市场有风险，投资需谨慎。</div>
      <div class="watermark">{_esc(generated_at) if generated_at else ""}</div>
    </div>
  </div>
</body>
</html>"""


def generate_portfolio_share_image(
    *,
    snapshot: Dict[str, Any],
    risk_report: Dict[str, Any],
    show_pnl_amount: bool = True,
    show_pnl_pct: bool = True,
    show_equity: bool = True,
) -> Optional[bytes]:
    """Build and render the portfolio share card PNG."""
    card_date = str(snapshot.get("as_of") or date.today().isoformat())

    # Today's P&L
    pnl = compute_day_pnl(snapshot)
    total_equity = float(snapshot.get("total_equity") or snapshot.get("total_market_value") or 0.0)

    # Sector allocation
    sector_concentration = (risk_report or {}).get("sector_concentration") or {}
    top_sectors = sector_concentration.get("top_sectors") or []

    # 7-day trend: portfolio vs SSE (dual line, as P&L card background)
    trend_svg = ""
    trend_pct = 0.0
    try:
        payload = build_sparkline_payload(snapshot, days=7)
        if len(payload["portfolio"]) >= 2:
            p0 = payload["portfolio"][0]
            p1 = payload["portfolio"][-1]
            trend_pct = (p1 / p0 - 1.0) * 100.0 if p0 else 0.0
            trend_svg = build_dual_line_svg(
                payload["portfolio"],
                payload["index"] if any(payload["index"]) else None,
                labels=payload["labels"],
                background=True,
            )
    except Exception as exc:  # pragma: no cover
        logger.warning("构建7日走势失败: %s", exc)

    # Position ratio = market value / total equity
    total_mv = float(snapshot.get("total_market_value") or 0.0)
    total_eq = float(snapshot.get("total_equity") or 0.0)
    position_ratio = (total_mv / total_eq * 100.0) if total_eq else 0.0

    # Main index
    main_index = fetch_main_index()

    position_count = _count_positions(snapshot)
    generated_at = datetime.now().strftime("%Y-%m-%d %H:%M")

    html_text = build_card_html(
        card_date=card_date,
        day_pnl_amount=pnl["day_pnl_amount"],
        day_pnl_pct=pnl["day_pnl_pct"],
        total_equity=total_equity,
        sectors=top_sectors,
        position_count=position_count,
        trend_svg=trend_svg,
        trend_pct=trend_pct,
        position_ratio=position_ratio,
        main_index=main_index,
        generated_at=generated_at,
        show_pnl_amount=show_pnl_amount,
        show_pnl_pct=show_pnl_pct,
        show_equity=show_equity,
    )
    return _html_to_png(html_text)
