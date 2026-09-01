# -*- coding: utf-8 -*-
"""拉取历史日线价格（腾讯K线接口，股票/ETF/可转债通用），存入 data/cache/prices_rebuild/"""
import json
import os
import sys
import time
import requests

SRC = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
CACHE_DIR = os.path.join(SRC, "data", "cache", "prices_rebuild")
os.makedirs(CACHE_DIR, exist_ok=True)

# 从导出的交易记录提取需拉价格的标的
TRADE_XLSX = r"E:\下载\A-ORDER\汇总持仓 (1).xlsx"
START = "2024-03-01"
END = "2026-08-27"

# 沪深前缀判定
def prefix(code: str) -> str:
    if code.startswith(("6", "5", "11", "113", "118", "501", "518", "513")):
        return "sh"
    return "sz"

def tz_kline(code: str, start=START, end=END, cnt=700):
    sym = prefix(code) + code
    url = f"https://web.ifzq.gtimg.cn/appstock/app/fqkline/get?param={sym},day,{start},{end},{cnt},"
    for attempt in range(3):
        try:
            r = requests.get(url, timeout=20, headers={"User-Agent": "Mozilla/5.0"})
            d = r.json()
            data = d.get("data", {})
            key = sym
            k = data.get(key, {}).get("day") or data.get(key, {}).get("qfqday") or []
            return k
        except Exception as e:
            if attempt == 2:
                raise
            time.sleep(2)
    return []

def load_targets():
    import openpyxl
    wb = openpyxl.load_workbook(TRADE_XLSX, data_only=True)
    ws = wb["交易记录"]
    held = {}
    for row in ws.iter_rows(min_row=2, values_only=True):
        if row[4] in ("买入", "卖出", "新股入帐", "股份转入", "转债转入"):
            held.setdefault(str(row[2]), row[3])
    return held

def main():
    targets = load_targets()
    print(f"待拉取标的: {len(targets)}")
    ok, fail = 0, []
    for i, (code, name) in enumerate(sorted(targets.items())):
        cache = os.path.join(CACHE_DIR, f"{code}.csv")
        try:
            k = tz_kline(code)
            with open(cache, "w", encoding="utf-8") as f:
                for row in k:
                    # row: [date, open, close, high, low, ...]
                    f.write(f"{row[0]},{row[2]}\n")
            print(f"[{i+1}/{len(targets)}] {code} {name}: {len(k)} 行 -> {cache}")
            ok += 1
        except Exception as e:
            print(f"[{i+1}/{len(targets)}] {code} {name} FAIL: {str(e)[:80]}")
            fail.append(code)
        time.sleep(0.4)
    print(f"\n完成: 成功 {ok}, 失败 {len(fail)}")
    if fail:
        print("失败列表:", fail)

if __name__ == "__main__":
    main()
