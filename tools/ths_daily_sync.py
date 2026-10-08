# -*- coding: utf-8 -*-
"""每日收盘后自动同步同花顺账本（由计划任务 DSA-ThsDailySync 触发）。

行为：
- 跳过周末（A 股休市）；法定节假日不单独建模，节假日执行同步为幂等操作、无害。
- 在线直拉账本接口（持仓 + 交易流水 + 资产曲线），无需手动导出文件。
- 登录失效时退出码 2 并写日志，便于排查（重新扫码登录即可恢复）。
"""
import sys
import os
import json
import logging
from datetime import date

BASE = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(0, BASE)

LOG_DIR = os.path.join(BASE, "logs")
os.makedirs(LOG_DIR, exist_ok=True)
LOG_FILE = os.path.join(LOG_DIR, "ths_daily_sync.log")
logging.basicConfig(
    filename=LOG_FILE,
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(message)s",
    encoding="utf-8",
)


def main() -> int:
    today = date.today()
    if today.weekday() >= 5:  # 周六/周日
        msg = "skip weekend %s" % today.isoformat()
        logging.info(msg)
        print("SKIP_WEEKEND")
        return 0

    try:
        from src.services.ths_sync.ths_sync_service import ThsSyncService

        svc = ThsSyncService()
        r = svc.sync(import_asset=True)
    except Exception as exc:  # noqa: BLE001
        logging.error("sync failed: %r", exc)
        print("SYNC_FAIL:", repr(exc))
        return 2

    summary = {
        "date": today.isoformat(),
        "accounts_scanned": r.get("accounts_scanned"),
        "trades_fetched": r.get("trades_fetched"),
        "positions_merged": r.get("positions_merged"),
        "total_cash": r.get("total_cash"),
        "asset_written": r.get("asset_written"),
    }
    logging.info("sync ok: %s", json.dumps(summary, ensure_ascii=False))
    print("SYNC_OK", json.dumps(summary, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    sys.exit(main())
