"""
持倉籌碼分析雷達 - dk edition
每天抓三大法人 + 分點買賣資料，計算外資耗盡訊號
推送到 Telegram + 儲存 JSON 供網頁顯示
"""

import requests
import urllib3
import json
import os
from datetime import datetime, timedelta
from collections import defaultdict

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# 設定區
# ============================================================
TELEGRAM_TOKEN  = os.environ.get("TELEGRAM_TOKEN", "8225398265:AAF8uJObOAfElE789AQPu6p7v6Y7XzbGFjk")
CHAT_ID         = os.environ.get("CHAT_ID", "8695864227")
FINMIND_TOKEN   = os.environ.get("FINMIND_TOKEN", "")


def load_holdings():
    """讀取 watchlist.json 的 holdings 清單"""
    wl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")
    try:
        with open(wl_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        holdings = [
            {"code": h["code"], "name": h.get("name", h["code"]), "is_etf": h.get("is_etf", False)}
            for h in data.get("holdings", [])
        ]
        if holdings:
            print(f"watchlist.json 載入：持倉 {len(holdings)} 支")
            return holdings
    except Exception as e:
        print(f"watchlist.json 載入失敗: {e}，使用預設清單")
    return [
        {"code": "2330", "name": "台積電",     "is_etf": False},
        {"code": "006208", "name": "富邦台50", "is_etf": True},
        {"code": "00878", "name": "國泰高股息","is_etf": True},
    ]


HOLDINGS = load_holdings()

HISTORY_FILE = "data/holding_history.json"
OUTPUT_FILE  = "data/holding_radar.json"

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
}
# ============================================================


def send_telegram(text: str):
    url = f"https://api.telegram.org/bot{TELEGRAM_TOKEN}/sendMessage"
    try:
        r = requests.post(url, json={
            "chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"
        }, timeout=10, verify=False)
        return r.json().get("ok", False)
    except Exception as e:
        print(f"Telegram 失敗: {e}")
        return False


def get_trading_date():
    """取得最近交易日（台灣時間 UTC+8）。
    Workflow 排在 UTC 8:00 AM = 台灣 4:00 PM，T86 已發佈，直接用當日。"""
    from datetime import timezone
    tw_now = datetime.now(timezone(timedelta(hours=8))).replace(tzinfo=None)
    d = tw_now
    if d.hour < 16:          # 台灣 4PM 前用前一交易日
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


# ── T86：當日三大法人 ────────────────────────────────────────
def fetch_institutional(code: str, date: str) -> dict:
    """
    T86 現為 19 欄（TWSE 新增外資自營商欄後索引位移）：
    [2-4] 外資(不含自營商)  [5-7] 外資自營商  [8-10] 投信
    [11] 自營商合計  [18] 三大法人合計
    """
    url = f"https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date={date}&selectType=ALL"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, verify=False)
        if r.status_code != 200:
            return {}
        data = r.json()
        if data.get("stat") != "OK":
            return {}
        for row in data.get("data", []):
            if len(row) < 19:
                continue
            if str(row[0]).strip() == code:
                def st(v):
                    try: return int(str(v).replace(",", "").strip()) // 1000
                    except: return 0
                return {
                    "foreign_buy":  st(row[2]),
                    "foreign_sell": st(row[3]),
                    "foreign_net":  st(row[4]),
                    "trust_buy":    st(row[8]),
                    "trust_sell":   st(row[9]),
                    "trust_net":    st(row[10]),
                    "dealer_net":   st(row[11]),
                    "total_net":    st(row[18]),   # 三大法人合計
                }
    except Exception as e:
        print(f"  T86 抓取失敗: {e}")
    return {}


# ── FinMind：當日三大法人（T86 被擋時的備援） ───────────────
def fetch_institutional_finmind(code: str, date: str) -> dict:
    """TWSE T86 在雲端 IP（如 GitHub Actions）常回傳空陣列，改用 FinMind 補抓當日資料。"""
    d_str = f"{date[:4]}-{date[4:6]}-{date[6:]}"
    url = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset":    "TaiwanStockInstitutionalInvestorsBuySell",
        "data_id":    code,
        "start_date": d_str,
        "end_date":   d_str,
    }
    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=20, verify=False)
        data = r.json()
        if data.get("status") != 200 or not data.get("data"):
            return {}
        result = {"foreign_net": 0, "trust_net": 0, "dealer_net": 0, "total_net": 0}
        for rec in data["data"]:
            net  = (rec.get("buy", 0) - rec.get("sell", 0)) // 1000
            name = rec.get("name", "")
            if name in ("Foreign_Investor", "Foreign_Dealer_Self"):
                result["foreign_net"] += net
            elif name == "Investment_Trust":
                result["trust_net"] += net
            elif name in ("Dealer_self", "Dealer_Hedging"):
                result["dealer_net"] += net
            result["total_net"] += net
        return result
    except Exception as e:
        print(f"  FinMind 當日三大法人抓取失敗: {e}")
        return {}


# ── FinMind：30 日歷史三大法人（補充 20 日集中度） ───────────
def fetch_finmind_history(code: str, days: int = 35) -> list:
    """
    FinMind TaiwanStockInstitutionalInvestorsBuySell（免費）
    回傳 [{"date":"20260504","foreign_net":9111,"trust_net":809,
            "dealer_net":377,"total_net":10298}, ...]
    """
    from datetime import date as date_cls
    end   = date_cls.today()
    start = end - timedelta(days=days + 10)
    url   = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset":    "TaiwanStockInstitutionalInvestorsBuySell",
        "data_id":    code,
        "start_date": start.strftime("%Y-%m-%d"),
        "end_date":   end.strftime("%Y-%m-%d"),
    }
    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=20, verify=False)
        data = r.json()
        if data.get("status") != 200:
            print(f"  FinMind 回傳狀態異常: {data.get('status')}")
            return []

        daily = defaultdict(lambda: {"foreign_net": 0, "trust_net": 0, "dealer_net": 0, "total_net": 0})
        for rec in data.get("data", []):
            d   = rec["date"].replace("-", "")
            buy = rec.get("buy", 0)
            sell = rec.get("sell", 0)
            net = (buy - sell) // 1000
            name = rec.get("name", "")
            if name in ("Foreign_Investor", "Foreign_Dealer_Self"):
                daily[d]["foreign_net"] += net
            elif name == "Investment_Trust":
                daily[d]["trust_net"] += net
            elif name in ("Dealer_self", "Dealer_Hedging"):
                daily[d]["dealer_net"] += net
            daily[d]["total_net"] += net

        return [{"date": d, **v} for d, v in sorted(daily.items())]
    except Exception as e:
        print(f"  FinMind 歷史抓取失敗: {e}")
        return []


# ── FinMind：融資融券歷史（免費，補充歷史融資餘額） ──────────────
def fetch_margin_history_finmind(code: str, days: int = 35) -> dict:
    """
    FinMind TaiwanStockMarginPurchaseShortSale（免費）
    回傳 {date_str: {"margin_balance": int, "short_balance": int}}
    """
    from datetime import date as date_cls
    end   = date_cls.today()
    start = end - timedelta(days=days + 10)
    url   = "https://api.finmindtrade.com/api/v4/data"
    params = {
        "dataset":    "TaiwanStockMarginPurchaseShortSale",
        "data_id":    code,
        "start_date": start.strftime("%Y-%m-%d"),
        "end_date":   end.strftime("%Y-%m-%d"),
    }
    if FINMIND_TOKEN:
        params["token"] = FINMIND_TOKEN
    try:
        r = requests.get(url, params=params, headers=HEADERS, timeout=20, verify=False)
        data = r.json()
        if data.get("status") != 200:
            return {}
        result = {}
        for rec in data.get("data", []):
            d = rec["date"].replace("-", "")
            result[d] = {
                "margin_balance": rec.get("MarginPurchaseBalance", 0),
                "short_balance":  rec.get("ShortSaleBalance", 0),
            }
        return result
    except Exception as e:
        print(f"  FinMind 融資歷史抓取失敗: {e}")
        return {}


# ── TWSE MI_MARGN：融資融券（免費，無需授權） ──────────────────
def fetch_margin(code: str, date: str) -> dict:
    """
    TWSE MI_MARGN 融資融券餘額
    欄位：[5]融資前日餘額 [6]融資今日餘額 [11]融券前日餘額 [12]融券今日餘額
    MI_MARGN 約下午 5-6 時才更新；若當日無資料自動退一交易日。
    """
    def si(v):
        try: return int(str(v).replace(",", "").strip())
        except: return 0

    def try_date(d_str: str) -> dict:
        url = f"https://www.twse.com.tw/rwd/zh/marginTrading/MI_MARGN?response=json&date={d_str}&selectType=ALL"
        try:
            r = requests.get(url, headers=HEADERS, timeout=20, verify=False)
            if r.status_code != 200:
                return {}
            data = r.json()
            if data.get("stat") != "OK":
                return {}
            # API returns {"tables": [{summary}, {stocks}]} structure
            all_rows = []
            for tbl in data.get("tables", []):
                all_rows.extend(tbl.get("data", []))
            # Also check top-level "data" key for compatibility
            all_rows.extend(data.get("data", []))
            for row in all_rows:
                if len(row) < 13:
                    continue
                if str(row[0]).strip() == code:
                    margin_prev  = si(row[5])
                    margin_today = si(row[6])
                    short_today  = si(row[12])
                    return {
                        "margin_balance": margin_today,
                        "margin_prev":    margin_prev,
                        "short_balance":  short_today,
                        "margin_shrink":  margin_today < margin_prev and margin_prev > 0,
                    }
        except Exception as e:
            print(f"  融資融券抓取失敗 ({d_str}): {e}")
        return {}

    result = try_date(date)
    if result:
        return result
    # 退一交易日再試（TWSE 當日資料延遲發布）
    prev = datetime.strptime(date, "%Y%m%d") - timedelta(days=1)
    while prev.weekday() >= 5:
        prev -= timedelta(days=1)
    return try_date(prev.strftime("%Y%m%d"))


# ── Yahoo Finance：當日成交量 ────────────────────────────────
def fetch_volume(code: str, date: str, vol_map: dict = None) -> int:
    """回傳指定交易日的成交量（張）。日期對應修正後的 fetch_volume_history。"""
    if vol_map is None:
        vol_map = fetch_volume_history(code)
    return vol_map.get(date, 0)


# ── Yahoo Finance：歷史成交量（補充 FinMind 歷史記錄） ────────
def fetch_volume_history(code: str) -> dict:
    """回傳 {date_str: volume_張} for last 3 months（以台灣時間對齊交易日）"""
    vol_map = {}
    for suffix in [".TW", ".TWO"]:
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{suffix}?interval=1d&range=3mo",
                headers=HEADERS, timeout=15, verify=False
            )
            result = r.json().get("chart", {}).get("result", [])
            if not result:
                continue
            timestamps = result[0].get("timestamp", [])
            vols = result[0].get("indicators", {}).get("quote", [{}])[0].get("volume", [])
            for ts, v in zip(timestamps, vols):
                if v:
                    d = (datetime.utcfromtimestamp(ts) + timedelta(hours=8)).strftime("%Y%m%d")
                    vol_map[d] = int(v / 1000)
            if vol_map:
                break
        except:
            continue
    return vol_map


# ── 歷史資料 I/O ────────────────────────────────────────────
def load_history() -> dict:
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {}


def save_history(history: dict):
    os.makedirs("data", exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


# ── 集中度 ──────────────────────────────────────────────────
def calc_concentration(history_list: list, days: int) -> float:
    """
    集中度 = 近N日三大法人合計買賣超 / 近N日總成交量 * 100
    跳過成交量為零的記錄（休市日或資料缺失）
    """
    recent = history_list[-days:] if len(history_list) >= days else history_list
    recent = [d for d in recent if d.get("volume", 0) > 0]
    if not recent:
        return 0.0
    net_sum = sum((d.get("total_net") or d.get("foreign_net") or 0) for d in recent)
    vol_sum = sum(d.get("volume", 0) for d in recent)
    if vol_sum == 0:
        return 0.0
    return round(net_sum / vol_sum * 100, 2)


# ── 主分析 ──────────────────────────────────────────────────
def analyze_holding(code: str, name: str, is_etf: bool, history: dict) -> dict:
    date = get_trading_date()
    print(f"  抓取 {code} {name} ({date})...")

    inst   = fetch_institutional(code, date)
    if not inst:
        print(f"  T86 無資料（可能被雲端 IP 擋掉），改用 FinMind 補抓 {date}...")
        inst = fetch_institutional_finmind(code, date)
    margin = fetch_margin(code, date)

    vol_map = fetch_volume_history(code)
    vol     = fetch_volume(code, date, vol_map)

    has_data  = bool(inst)
    today_data = {**inst, **margin, "volume": vol, "date": date}

    # 載入 / 補充歷史
    h_list = history.get(code, [])

    # 清除 total_net 明顯錯誤的舊記錄（舊 T86 欄位映射 row[13] 殘留的垃圾值）
    # 判斷：total_net 存在但絕對值 < foreign_net 的 30%，且 foreign_net > 500 張
    def is_bad_total_net(entry: dict) -> bool:
        tn = entry.get("total_net")
        fn = entry.get("foreign_net")
        if tn is None or fn is None:
            return False
        if abs(fn) < 500:
            return False
        return abs(tn) < abs(fn) * 0.3

    bad_dates = {d["date"] for d in h_list if is_bad_total_net(d)}
    if bad_dates:
        print(f"  清除 {len(bad_dates)} 筆錯誤 total_net 記錄：{sorted(bad_dates)}")
        h_list = [d for d in h_list if d["date"] not in bad_dates]

    existing_dates = {d["date"] for d in h_list}

    # 修正既有記錄中因舊版日期偏移造成的 volume=0
    for entry in h_list:
        if not entry.get("volume") and entry["date"] in vol_map:
            entry["volume"] = vol_map[entry["date"]]

    # 若近 30 天有效筆數 < 20，或剛清除了錯誤記錄，從 FinMind 補充
    cutoff = (datetime.strptime(date, "%Y%m%d") - timedelta(days=30)).strftime("%Y%m%d")
    valid_count = len([d for d in h_list
                        if d["date"] >= cutoff and d.get("volume", 0) > 0 and d.get("total_net") is not None])
    if valid_count < 20:
        print(f"  近 30 天有效歷史 {valid_count} 日，從 FinMind 補充...")
        fin_records = fetch_finmind_history(code)
        for rec in fin_records:
            d = rec["date"]
            if d not in existing_dates:
                h_list.append({
                    **rec,
                    "volume": vol_map.get(d, 0),
                })
                existing_dates.add(d)
        print(f"  FinMind 補充後歷史筆數: {len(h_list)}")

    # 補充歷史融資餘額（FinMind 免費，有助於計算融資趨勢）
    missing_margin = [d for d in h_list if d.get("margin_balance") is None]
    if missing_margin:
        margin_hist = fetch_margin_history_finmind(code)
        if margin_hist:
            for entry in h_list:
                if entry.get("margin_balance") is None and entry["date"] in margin_hist:
                    entry.update(margin_hist[entry["date"]])

    # 加入今日（有效資料才寫入，避免休市日空記錄）
    if has_data and date not in existing_dates:
        h_list.append(today_data)

    h_list = sorted(h_list, key=lambda x: x["date"])[-30:]
    history[code] = h_list

    # 指標
    foreign_net    = today_data.get("foreign_net", 0)
    total_net      = today_data.get("total_net",   0)
    margin_balance = today_data.get("margin_balance")
    margin_shrink  = today_data.get("margin_shrink", False)
    short_balance  = today_data.get("short_balance")
    conc_5d        = calc_concentration(h_list, 5)
    conc_20d       = calc_concentration(h_list, 20)

    # 連續買超/賣超天數
    streak = 0
    for d in reversed(h_list):
        net = d.get("foreign_net", 0)
        if foreign_net <= 0 and net <= 0:
            streak -= 1
        elif foreign_net > 0 and net > 0:
            streak += 1
        else:
            break

    signals = {
        "foreign_flip":    foreign_net > 0,
        "total_positive":  total_net > 0,
        "conc5_rising":    len(h_list) >= 2 and
                           conc_5d > calc_concentration(h_list[:-1], 5),
        "conc20_positive": conc_20d > 0,
        "margin_shrink":   margin_shrink,   # 融資餘額下降 = 散戶放棄（替代家數差）
        "price_support":   vol > 0,
    }

    return {
        "code":           code,
        "name":           name,
        "is_etf":         is_etf,
        "date":           date,
        "foreign_net":    foreign_net,
        "total_net":      total_net,
        "margin_balance": margin_balance,
        "short_balance":  short_balance,
        "margin_shrink":  margin_shrink,
        "conc_5d":        conc_5d,
        "conc_20d":       conc_20d,
        "streak":         streak,
        "signals":        signals,
        "exhaustion":     sum(signals.values()),
        "history_5d":     [{"date": d["date"],
                            "foreign_net":    d.get("foreign_net", 0),
                            "total_net":      d.get("total_net", 0),
                            "margin_balance": d.get("margin_balance")}
                           for d in h_list[-5:]],
    }


# ── Telegram 格式 ────────────────────────────────────────────
def format_telegram(results: list, date: str) -> str:
    lines = [f"📊 <b>持倉籌碼分析｜{date[:4]}/{date[4:6]}/{date[6:]}</b>"]

    for r in results:
        fn   = r["foreign_net"]
        tn   = r["total_net"]
        mb   = r.get("margin_balance")
        sb   = r.get("short_balance")
        c5   = r["conc_5d"]
        c20  = r["conc_20d"]
        exh  = r["exhaustion"]
        sigs = r["signals"]
        streak = r["streak"]

        def arrow(v):  return "🟢" if v > 0 else "🔴" if v < 0 else "⚪"
        def fmt(v):    return f"+{v:,}" if v > 0 else f"{v:,}"

        lines.append(f"\n{'─'*20}")
        lines.append(f"<b>{r['code']} {r['name']}</b>")

        if r["is_etf"]:
            lines.append("（ETF — 追蹤大盤，無主力洗盤邏輯）")
            lines.append(f"外資動向：{arrow(fn)} {fmt(fn)} 張")
            lines.append(f"三大法人：{arrow(tn)} {fmt(tn)} 張")
            lines.append(f"5日集中：{c5:+.2f}%")
        else:
            lines.append(f"① 外資買賣超：{arrow(fn)} {fmt(fn)} 張 {'✅' if sigs['foreign_flip'] else '❌'}")
            lines.append(f"② 三大法人：{arrow(tn)} {fmt(tn)} 張 {'✅' if sigs['total_positive'] else '❌'}")
            lines.append(f"③ 5日集中：{c5:+.2f}% {'✅' if sigs['conc5_rising'] else '❌'}")
            lines.append(f"④ 20日集中：{c20:+.2f}% {'✅' if sigs['conc20_positive'] else '❌'}")
            mb_str = f"{mb:,}" if mb is not None else "--"
            sb_str = f"{sb:,}" if sb is not None else "--"
            lines.append(f"⑤ 融資餘額：{mb_str}張（融券{sb_str}）{'✅' if sigs['margin_shrink'] else '❌'}")
            lines.append(f"⑥ 守支撐：{'✅' if sigs['price_support'] else '❌'}")

            streak_str = (f"連續賣超 {abs(streak)} 天" if streak < 0 else
                          f"連續買超 {streak} 天" if streak > 0 else "今日翻轉")
            lines.append(f"外資：{streak_str}")

            if exh >= 5:
                lines.append(f"🔥 <b>賣壓耗盡訊號：{exh}/6 強烈確認</b>")
            elif exh >= 3:
                lines.append(f"⚡ 賣壓耗盡訊號：{exh}/6 觀察中")
            else:
                lines.append(f"❌ 賣壓耗盡訊號：{exh}/6 尚未確認")

            hist5 = r.get("history_5d", [])
            if len(hist5) >= 2:
                trend = [d.get("total_net", d.get("foreign_net", 0)) for d in hist5]
                trend_str = " → ".join([fmt(v) for v in trend])
                lines.append(f"近期三法人：{trend_str}")

    lines.append(f"\n{'─'*20}")
    lines.append("⚠️ 僅供參考，請自行判斷")
    return "\n".join(lines)


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 持倉籌碼雷達啟動...")
    date = get_trading_date()
    print(f"交易日：{date}")

    history = load_history()

    results = []
    for h in HOLDINGS:
        r = analyze_holding(h["code"], h["name"], h["is_etf"], history)
        results.append(r)
        print(f"  {h['code']}: 外資{r['foreign_net']:+,}張 三法人{r['total_net']:+,}張 5d{r['conc_5d']:+.2f}% 20d{r['conc_20d']:+.2f}% 耗盡{r['exhaustion']}/6")

    save_history(history)

    out = {
        "generated": datetime.now().strftime("%Y/%m/%d %H:%M"),
        "date": date,
        "holdings": results,
    }
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"JSON 已存到 {OUTPUT_FILE}")

    msg = format_telegram(results, date)
    ok = send_telegram(msg)
    print(f"Telegram：{'成功' if ok else '失敗'}")


if __name__ == "__main__":
    main()
