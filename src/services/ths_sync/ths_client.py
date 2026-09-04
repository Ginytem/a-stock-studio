# -*- coding: utf-8 -*-
"""同花顺投资账本客户端：扫码登录 + 数据拉取。

技术要点：
- 扫码登录走 upass.10jqka.com.cn（creatCode / creatImg / getInfoNew）
- 账本业务接口走 tzzb.10jqka.com.cn/caishen_httpserver，需 POST + 公共参数
- 请求必须用 curl_cffi（模拟 Chrome TLS 指纹），普通 requests/urllib 会被 403
- 登录态（cookie）持久化到本地 JSON 文件
"""
import json
import os
import time
from typing import Any, Dict, List, Optional

from curl_cffi import requests as cr

BASE_UPASS = "https://upass.10jqka.com.cn"
BASE_TZZB = "https://tzzb.10jqka.com.cn"
API_PATH = "/caishen_httpserver/tzzb/caishen_fund"

DEFAULT_HEADERS = {
    "Referer": BASE_TZZB + "/pc/index.html",
    "Origin": BASE_TZZB,
}


class ThsLoginError(RuntimeError):
    """扫码登录失败或登录态失效"""


class ThsSession:
    """封装 curl_cffi 会话与登录态持久化。"""

    def __init__(self, cookie_file: str):
        self.cookie_file = cookie_file
        self.session: Optional[cr.Session] = None

    # ------------------------------------------------------------------
    # 会话 / 登录态
    # ------------------------------------------------------------------
    def _make_session(self) -> cr.Session:
        s = cr.Session(impersonate="chrome")
        s.headers.update(DEFAULT_HEADERS)
        saved = self._load_cookies()
        for name, value in saved.items():
            s.cookies.set(name, value, domain="10jqka.com.cn", path="/")
        self.session = s
        return s

    def _load_cookies(self) -> Dict[str, str]:
        if os.path.exists(self.cookie_file):
            try:
                with open(self.cookie_file, "r", encoding="utf-8") as f:
                    raw = json.load(f)
                # 兼容嵌套结构：{"cookies": {...}}
                if isinstance(raw, dict) and isinstance(raw.get("cookies"), dict):
                    return raw["cookies"]
                if isinstance(raw, dict):
                    return raw
                return {}
            except Exception:  # noqa: BLE001
                return {}
        return {}

    def _save_cookies(self) -> None:
        if self.session is None:
            return
        cookies = {c.name: c.value for c in self.session.cookies}
        with open(self.cookie_file, "w", encoding="utf-8") as f:
            json.dump(cookies, f, ensure_ascii=False, indent=2)

    def is_logged_in(self) -> bool:
        cookies = self._load_cookies()
        return bool(cookies.get("userid") and cookies.get("ticket"))

    def clear_login(self) -> None:
        if os.path.exists(self.cookie_file):
            os.remove(self.cookie_file)
        self.session = None

    def _s(self) -> cr.Session:
        if self.session is None:
            self._make_session()
        return self.session

    # ------------------------------------------------------------------
    # 扫码登录
    # ------------------------------------------------------------------
    def create_qrcode(self) -> Dict[str, Any]:
        """创建扫码会话，返回 {qrid, qr_image_bytes}。"""
        s = self._make_session()
        r = s.post(BASE_UPASS + "/scan/creatCode", timeout=15)
        data = r.json()
        qrid = data.get("qrid")
        if not qrid:
            raise ThsLoginError("creatCode failed: " + json.dumps(data, ensure_ascii=False))
        img = s.get(BASE_UPASS + "/scan/creatImg", params={"qrid": qrid}, timeout=15).content
        return {"qrid": qrid, "qr_image": img}

    def poll_login(self, qrid: str, timeout: float = 180.0) -> bool:
        """轮询扫码结果，返回是否登录成功（成功后自动保存 cookie）。"""
        s = self._s()
        deadline = time.time() + timeout
        while time.time() < deadline:
            time.sleep(3)
            try:
                r = s.post(
                    BASE_UPASS + "/scan/getInfoNew",
                    data={
                        "qrid": qrid,
                        "state": "1",
                        "source": "pc_web",
                        "page_source": "web_screen",
                        "request_type": "login",
                    },
                    timeout=12,
                )
                data = r.json()
            except Exception:  # noqa: BLE001
                continue
            status = int(data.get("status") or 0)
            # status: 0=需重新取码 1/2=等待 3=登录成功
            if status == 3:
                self._save_cookies()
                return True
            if status == 0:
                raise ThsLoginError("qrcode expired, please retry")
        return False

    # ------------------------------------------------------------------
    # 账本数据接口
    # ------------------------------------------------------------------
    def _api(self, path: str, **params: Any) -> Dict[str, Any]:
        s = self._s()
        cookies = self._load_cookies()
        if not (cookies.get("userid") and cookies.get("ticket")):
            raise ThsLoginError("not logged in")
        body = {
            "terminal": "1",
            "version": "0.0.0",
            "userid": cookies.get("userid", ""),
            "user_id": cookies.get("userid", ""),
        }
        body.update({k: str(v) for k, v in params.items() if v is not None})
        r = s.post(BASE_TZZB + API_PATH + path, data=body, timeout=20)
        if r.status_code != 200:
            raise ThsLoginError(f"api {path} -> {r.status_code}")
        try:
            return r.json()
        except ValueError:
            raise ThsLoginError(f"api {path} invalid json: {r.text[:120]}")

    def fetch_accounts(self) -> List[Dict[str, Any]]:
        data = self._api("/pc/account/v1/account_list")
        ex = data.get("ex_data") or {}
        out = []
        for acc in ex.get("common") or []:
            out.append({
                "type": "common",
                "fund_key": str(acc.get("fund_key")),
                "name": acc.get("manualname") or acc.get("brokername") or "",
                "broker_name": acc.get("brokername") or "",
            })
        for acc in ex.get("fund") or []:
            out.append({
                "type": "fund",
                "fund_key": str(acc.get("fundId")),
                "name": acc.get("fundname") or "",
                "broker_name": acc.get("fundname") or "",
            })
        return out

    def fetch_positions(self, fund_key: str) -> Dict[str, Any]:
        data = self._api("/pc/asset/v1/stock_position", fund_key=fund_key)
        ex = data.get("ex_data") or {}
        positions = []
        for p in ex.get("position") or []:
            positions.append({
                "code": p.get("code"),
                "name": p.get("name"),
                "cost": float(p.get("cost") or 0),
                "quantity": float(p.get("count") or 0),
                "price": float(p.get("price") or 0),
                "value": float(p.get("value") or 0),
                "hold_profit": float(p.get("hold_profit") or 0),
                "hold_rate": float(p.get("hold_rate") or 0),
                # 当日盈亏（账本口径：pre_profit=当日盈亏金额，pre_rate=当日盈亏率）
                "day_pnl": float(p.get("pre_profit") or 0),
                "day_pnl_pct": float(p.get("pre_rate") or 0),
                "hold_days": int(float(p.get("hold_days") or 0)),
                "market": p.get("market"),
            })
        return {
            "money_remain": float(ex.get("money_remain") or 0),
            "positions": positions,
            "total_asset": float(ex.get("total_asset") or 0),
        }

    def fetch_trades(
        self,
        fund_key: str,
        page: int = 1,
        count: int = 100,
        sort_type: str = "0",
        sort_order: str = "desc",
        start_date: Optional[str] = None,
        end_date: Optional[str] = None,
    ) -> List[Dict[str, Any]]:
        data = self._api(
            "/pc/account/v2/get_money_history",
            fund_key=fund_key,
            page=page,
            count=count,
            sort_type=sort_type,
            sort_order=sort_order,
            start_date=start_date,
            end_date=end_date,
        )
        ex = data.get("ex_data") or {}
        out = []
        for t in ex.get("list") or []:
            out.append({
                "code": t.get("code"),
                "name": t.get("name"),
                "trade_date": t.get("entry_date"),
                "trade_time": t.get("entry_time"),
                "side": "buy" if str(t.get("op")) in ("1", "5", "3") else "sell",
                "op": t.get("op"),
                "op_name": t.get("op_name"),
                "quantity": float(t.get("entry_count") or 0),
                "price": float(t.get("entry_price") or 0),
                "amount": float(t.get("entry_money") or 0),
                "fee": float(t.get("fee_total") or 0),
                "commission": float(t.get("commission") or 0),
                "transfer_fee": float(t.get("transfer_fee") or 0),
                "trans_no": t.get("trans_no"),
                "vid": t.get("vid"),
                "account_id": t.get("account_id"),
                "market": t.get("market_code"),
            })
        return out

    def fetch_asset_trend(self, fund_key: str) -> List[Dict[str, Any]]:
        data = self._api("/pc/asset/v1/asset_trend", fund_key=fund_key)
        ex = data.get("ex_data") or {}
        points = ex.get("total_asset") or ex.get("data") or ex.get("list") or []
        out = []
        for p in points:
            out.append({
                "date": p.get("date"),
                "asset": float(p.get("asset") or 0),
                "fund_in": float(p.get("fundIn") or 0),
                "fund_out": float(p.get("fundOut") or 0),
                "profit": float(p.get("profit") or 0),
            })
        return out
