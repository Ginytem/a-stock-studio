# -*- coding: utf-8 -*-
"""从同花顺导出的交易记录重建完整历史每日资产，写入 portfolio_daily_snapshots。

原理：
1. 逐笔回放交易（含银证转账/逆回购/派息/新股入帐），得到每日持仓数量 + 现金
2. 用腾讯K线不复权收盘价估算每日持仓市值
3. 现金校准：使最终现金与账本当前现金一致
4. 写入快照表（account_id=2, cost_method='fifo'）
"""
import json
import os
import sqlite3
import sys
from collections import defaultdict
from datetime import date, datetime

import openpyxl

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DB = os.path.join(SRC, "data", "stock_analysis.db")
PRICE_DIR = os.path.join(SRC, "data", "cache", "prices_rebuild")
TRADE_XLSX = r"E:\下载\A-ORDER\汇总持仓 (1).xlsx"

# 账本当前现金（用于校准）
TARGET_CASH = 1684.86
ACCOUNT_ID = 2  # 百福具臻账户
COST_METHOD = "fifo"

# 持仓类（数量变动 + 现金变动）
POS_IN = {"买入", "新股入帐", "股份转入", "转债转入"}
POS_OUT = {"卖出"}
# 纯现金类
CASH_ONLY = {"银行转证券", "证券转银行", "银行转存", "银行转取",
             "股息个税征收", "除权除息"}
# 外部现金流（银证转账/银行存取）：影响收益率计算的入金/出金
EXTERNAL_CASH = {"银行转证券", "证券转银行", "银行转存", "银行转取"}
# 逆回购类（现金变动）
REPO = None  # 前缀匹配


def load_trades():
    wb = openpyxl.load_workbook(TRADE_XLSX, data_only=True)
    ws = wb["交易记录"]
    trades = []
    for r in ws.iter_rows(min_row=2, values_only=True):
        if not r[0]:
            continue
        trades.append({
            "date": str(r[0]),
            "time": str(r[1]) if r[1] else "",
            "code": str(r[2]).strip() if r[2] else "",
            "cat": str(r[4]),
            "qty": float(r[5] or 0),
            "amt": float(r[7] or 0),
        })
    trades.sort(key=lambda t: (t["date"], t["time"]))
    return trades


def load_prices():
    """code -> {date_str: close}"""
    prices = {}
    for fn in os.listdir(PRICE_DIR):
        if not fn.endswith(".csv"):
            continue
        code = fn[:-4]
        d = {}
        with open(os.path.join(PRICE_DIR, fn), encoding="utf-8") as f:
            for line in f:
                line = line.strip()
                if not line:
                    continue
                parts = line.split(",")
                if len(parts) < 2:
                    continue
                d[parts[0]] = float(parts[1])
        prices[code] = d
    return prices


def main():
    trades = load_trades()
    prices = load_prices()
    print(f"交易 {len(trades)} 笔, 价格标的 {len(prices)} 只")

    # 所有交易日（价格并集）
    all_dates = set()
    for d in prices.values():
        all_dates.update(d.keys())
    trade_days = sorted(all_dates)
    print(f"交易日范围: {trade_days[0]} ~ {trade_days[-1]}, 共 {len(trade_days)} 天")

    # 按日期索引交易
    trades_by_date = defaultdict(list)
    for t in trades:
        trades_by_date[t["date"]].append(t)

    # 回放
    qty = defaultdict(float)
    cash = 0.0
    snapshots = []  # (date, qty_snapshot, cash, net_external_cashflow)
    for d in trade_days:
        ext_cash = 0.0  # 外部现金流：银证转账/银行存取（入金正、出金负）
        for t in trades_by_date.get(d, []):
            cat = t["cat"]
            if cat in POS_IN:
                qty[t["code"]] += t["qty"]
                cash += t["amt"]
            elif cat in POS_OUT:
                qty[t["code"]] -= t["qty"]
                cash += t["amt"]
            elif cat in CASH_ONLY:
                cash += t["amt"]
                if cat in EXTERNAL_CASH:
                    ext_cash += t["amt"]
            elif cat.startswith(("融券", "质押", "通用", "拆出")):
                # 逆回购：本金隔日归还，不改变总资产，仅利息（几元）可忽略，不计入现金
                pass
            else:
                print(f"未处理类别: {cat} @ {d}")
        snapshots.append((d, dict(qty), cash, ext_cash))

    # 现金校准
    final_cash = snapshots[-1][2]
    adj = TARGET_CASH - final_cash
    print(f"重建最终现金: {final_cash:.2f}, 账本现金: {TARGET_CASH}, 校准偏移: {adj:.2f}")

    # 估值写库
    con = sqlite3.connect(DB)
    cur = con.cursor()
    inserted = 0
    # 删除旧的重建范围（2026-08-27 之前）
    cur.execute(
        "DELETE FROM portfolio_daily_snapshots WHERE account_id=? AND cost_method=? AND snapshot_date < '2026-08-28'",
        (ACCOUNT_ID, COST_METHOD),
    )
    for d, q, cash_raw, ext_cash in snapshots:
        # 前向填充价格
        mv = 0.0
        for code, cnt in q.items():
            if abs(cnt) < 1e-9 or not code:
                continue
            close = _ff_price(prices.get(code, {}), d)
            if close is None:
                continue
            mv += cnt * close
        cash_adj = cash_raw + adj
        equity = mv + cash_adj
        if equity <= 0:
            continue
        now = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        payload = json.dumps({"source": "ths_trade_rebuild", "snapshot_date": d,
                              "net_cashflow": round(ext_cash, 2)})
        cur.execute(
            """INSERT OR REPLACE INTO portfolio_daily_snapshots
            (account_id, snapshot_date, cost_method, base_currency,
             total_cash, total_market_value, total_equity,
             unrealized_pnl, realized_pnl, fee_total, tax_total, fx_stale,
             payload, created_at, updated_at)
            VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)""",
            (ACCOUNT_ID, d, COST_METHOD, "CNY",
             round(cash_adj, 2), round(mv, 2), round(equity, 2),
             0.0, 0.0, 0.0, 0.0, 0,
             payload, now, now),
        )
        inserted += 1
    con.commit()
    print(f"写入 {inserted} 条快照")

    # 验证
    row = cur.execute(
        "SELECT snapshot_date, total_equity, total_cash, total_market_value FROM portfolio_daily_snapshots "
        "WHERE account_id=? AND cost_method=? ORDER BY snapshot_date DESC LIMIT 3",
        (ACCOUNT_ID, COST_METHOD),
    ).fetchall()
    for r in row:
        print("  最新:", r)
    n = cur.execute(
        "SELECT COUNT(*) FROM portfolio_daily_snapshots WHERE account_id=? AND cost_method=?",
        (ACCOUNT_ID, COST_METHOD),
    ).fetchone()[0]
    print("账户快照总数:", n)
    con.close()


def _ff_price(d: dict, day: str):
    if day in d:
        return d[day]
    # 向前找最近一天
    for offset in range(1, 15):
        cand = _prev_trading_day(day, offset)
        if cand in d:
            return d[cand]
    return None


def _prev_trading_day(day: str, n: int):
    dt = datetime.strptime(day, "%Y-%m-%d")
    for _ in range(n):
        dt = dt - __import__("datetime").timedelta(days=1)
        while dt.weekday() >= 5:
            dt = dt - __import__("datetime").timedelta(days=1)
    return dt.strftime("%Y-%m-%d")


if __name__ == "__main__":
    main()
