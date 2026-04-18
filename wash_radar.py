"""
洗籌碼雷達 - dk edition
監控自選股的洗籌碼狀態與突破訊號
每天收盤後 4:30 執行，推送到 Telegram

安裝：pip install requests
"""

import re
import os
import requests
import urllib3
import json
import re
import os
import time
from datetime import datetime, timedelta

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# 設定區
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8225398265:AAF8uJObOAfElE789AQPu6p7v6Y7XzbGFjk")
CHAT_ID        = os.environ.get("CHAT_ID", "8695864227")

# 從 watchlist.json 讀取清單（與網頁同步）
def load_watchlist():
    """讀取 watchlist.json，回傳 holdings 和 watchlist"""
    wl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")
    try:
        with open(wl_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        holdings = [h["code"] for h in data.get("holdings", [])]
        etf_codes = {h["code"] for h in data.get("holdings", []) if h.get("is_etf")}
        watchlist = [s["code"] for s in data.get("watchlist", [])]
        print(f"watchlist.json 載入：持倉 {len(holdings)} 支，自選 {len(watchlist)} 支")
        return holdings, etf_codes, watchlist
    except Exception as e:
        print(f"watchlist.json 載入失敗: {e}，使用預設清單")
        # 備用清單
        return (
            ["2330", "006208", "00878"],
            {"006208", "00878"},
            ["2317", "2303", "3006", "3034", "3545", "8016", "4961",
             "2454", "6196", "3037", "8046", "3189", "2049", "2634",
             "3005", "2376", "3231", "1326", "1301", "2327", "2308",
             "2054", "2002", "2027", "2014", "1504", "1513", "1503",
             "9941", "5871", "8436", "1707", "2379", "3558", "2618",
             "2610", "2603", "2385", "1477", "4164", "1720", "1752",
             "4114", "1216", "2357", "2412"]
        )

HOLDINGS, ETF_CODES, WATCHLIST = load_watchlist()

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


def get_stock_name(code: str, name_map: dict) -> str:
    return name_map.get(code, code)


def fetch_twse_daily(date_str: str) -> dict:
    """抓 TWSE 當日所有上市股票收盤資料"""
    url = f"https://www.twse.com.tw/rwd/zh/afterTrading/MI_INDEX?response=json&date={date_str}&type=ALLBUT0999"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, verify=False)
        data = r.json()
        result = {}
        # 找個股行情表（表9）
        for table in data.get("tables", []):
            fields = table.get("fields", [])
            if "收盤價" in fields and "成交股數" in fields:
                rows = table.get("data", [])
                for row in rows:
                    if len(row) < 9: continue
                    code = str(row[0]).strip()
                    try:
                        vol   = float(str(row[2]).replace(",", ""))
                        close = float(str(row[8]).replace(",", ""))
                        high  = float(str(row[6]).replace(",", ""))
                        low   = float(str(row[7]).replace(",", ""))
                        open_ = float(str(row[5]).replace(",", ""))
                        result[code] = {
                            "close": close, "open": open_,
                            "high": high, "low": low, "vol": vol
                        }
                    except: continue
        return result
    except Exception as e:
        print(f"  TWSE 日線失敗: {e}")
        return {}


def fetch_tpex_daily(date_str: str) -> dict:
    """抓 TPEx 當日所有上櫃股票收盤資料"""
    d = datetime.strptime(date_str, "%Y%m%d")
    tpex_date = f"{d.year-1911}/{d.month:02d}/{d.day:02d}"
    url = f"https://www.tpex.org.tw/web/stock/aftertrading/daily_close_quotes/stk_close_result.php?l=zh-tw&o=json&d={tpex_date}&s=0,asc"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, verify=False)
        text = r.content.decode("utf-8-sig", errors="ignore").strip()
        if not text or text[0] not in "{[":
            return {}
        data = json.loads(text)
        rows = data.get("aaData", [])
        result = {}
        for row in rows:
            if len(row) < 8: continue
            code = str(row[0]).strip()
            try:
                def sf(v): 
                    try: return float(str(v).replace(",","").strip())
                    except: return 0
                result[code] = {
                    "close": sf(row[2]), "open": sf(row[4]),
                    "high": sf(row[5]), "low": sf(row[6]),
                    "vol": sf(row[1]) * 1000
                }
            except: continue
        return result
    except Exception as e:
        print(f"  TPEx 日線失敗: {e}")
        return {}


def fetch_history(code: str, days: int = 25) -> list:
    """用 Yahoo Finance 抓個股日線（台股加.TW，上櫃加.TWO）"""
    # 先試上市 .TW，失敗再試上櫃 .TWO
    for suffix in [".TW", ".TWO"]:
        url = f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{suffix}?interval=1d&range=3mo"
        try:
            r = requests.get(url, headers=HEADERS, timeout=15, verify=False)
            if r.status_code != 200:
                continue
            data = r.json()
            result_list = data.get("chart", {}).get("result", [])
            if not result_list:
                continue
            quotes = result_list[0].get("indicators", {}).get("quote", [{}])[0]
            closes = quotes.get("close", [])
            opens  = quotes.get("open",  [])
            highs  = quotes.get("high",  [])
            lows   = quotes.get("low",   [])
            vols   = quotes.get("volume",[])
            result = []
            for i in range(len(closes)):
                if closes[i] is None: continue
                result.append({
                    "close": closes[i] or 0,
                    "open":  opens[i]  or 0,
                    "high":  highs[i]  or 0,
                    "low":   lows[i]   or 0,
                    "vol":   vols[i]   or 0,
                })
            if result:
                return result[-days:]
        except: continue
    return []


def analyze_stock(code: str, history: list) -> dict:
    """
    分析洗籌碼評分
    回傳：score(0-10), signals(list), status
    """
    if len(history) < 10:
        return {"score": 0, "signals": [], "status": "資料不足"}

    closes = [d["close"] for d in history]
    vols   = [d["vol"]   for d in history]
    highs  = [d["high"]  for d in history]
    lows   = [d["low"]   for d in history]

    today  = history[-1]
    score  = 0
    signals = []

    # 均量（前20日，不含今日）
    vol_ma20 = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else sum(vols[:-1]) / max(len(vols)-1, 1)
    # 近10日均量（洗籌碼期）
    vol_ma10 = sum(vols[-11:-1]) / 10 if len(vols) >= 11 else vol_ma20

    today_vol   = today["vol"]
    today_close = today["close"]
    today_high  = today["high"]
    today_low   = today["low"]
    today_open  = today["open"]

    # 近20日高點
    recent_high = max(highs[-21:-1]) if len(highs) >= 21 else max(highs[:-1])
    # MA20
    ma20 = sum(closes[-21:-1]) / 20 if len(closes) >= 21 else sum(closes[:-1]) / max(len(closes)-1, 1)
    # MA5
    ma5  = sum(closes[-6:-1]) / 5 if len(closes) >= 6 else today_close

    # ── 洗籌碼偵測 ──────────────────────────────────────────
    # 條件1：近10日量縮（均量 < 整體均量 70%）
    if vol_ma10 < vol_ma20 * 0.70:
        score += 2
        signals.append("📉 量縮中（近10日均量縮 30%+）")

    # 條件2：今日爆量（> 均量 150%）→ 可能突破
    if today_vol > vol_ma20 * 1.5:
        score += 3
        signals.append(f"💥 今日爆量（{today_vol/vol_ma20:.1f}x 均量）")

    # 條件3：今日收紅且收盤偏高（收盤在今日範圍上半段）
    day_range = today_high - today_low
    if day_range > 0 and today_close > today_open:
        closing_strength = (today_close - today_low) / day_range
        if closing_strength > 0.6:
            score += 2
            signals.append(f"🕯️ 收盤強勢（收在高點 {closing_strength:.0%}）")

    # 條件4：突破近20日高點
    if today_close > recent_high:
        score += 2
        signals.append(f"🚀 突破近20日高點（{recent_high:.1f}）")
    elif today_close > recent_high * 0.98:
        score += 1
        signals.append(f"⚡ 逼近近20日高點（{recent_high:.1f}）")

    # 條件5：守在 MA20 上方
    if today_low > ma20 * 0.995:
        score += 1
        signals.append(f"✅ 守住 MA20（{ma20:.1f}）")

    # 判斷狀態
    if score >= 7:
        status = "🔥 強烈突破訊號"
    elif score >= 5:
        status = "⚡ 突破訊號"
    elif score >= 3:
        status = "🌀 洗籌碼整理中"
    elif vol_ma10 < vol_ma20 * 0.70:
        status = "💤 量縮整理"
    else:
        status = "—"

    return {
        "score": score,
        "signals": signals,
        "status": status,
        "close": today_close,
        "vol_ratio": today_vol / vol_ma20 if vol_ma20 > 0 else 0,
        "ma20": ma20,
        "recent_high": recent_high,
    }


def analyze_etf(code: str, history: list) -> dict:
    """ETF 加碼時機分析（不用洗籌碼邏輯）"""
    if len(history) < 10:
        return {"score": 0, "signals": [], "status": "資料不足"}

    closes = [d["close"] for d in history]
    vols   = [d["vol"]   for d in history]
    highs  = [d["high"]  for d in history]

    today  = history[-1]
    score  = 0
    signals = []

    ma20 = sum(closes[-21:-1]) / 20 if len(closes) >= 21 else sum(closes[:-1]) / max(len(closes)-1,1)
    ma5  = sum(closes[-6:-1])  / 5  if len(closes) >= 6  else today["close"]
    vol_ma20 = sum(vols[-21:-1]) / 20 if len(vols) >= 21 else sum(vols[:-1]) / max(len(vols)-1,1)
    today_close = today["close"]
    today_low   = today["low"]
    today_vol   = today["vol"]

    # 加碼訊號1：回踩 MA20 支撐（距離 MA20 在 2% 以內）
    if ma20 > 0 and abs(today_close - ma20) / ma20 < 0.02:
        score += 3
        signals.append(f"📍 回踩 MA20 支撐（{ma20:.2f}）")

    # 加碼訊號2：量縮後今日放量
    vol_ma10 = sum(vols[-11:-1]) / 10 if len(vols) >= 11 else vol_ma20
    if vol_ma10 < vol_ma20 * 0.75 and today_vol > vol_ma20 * 1.2:
        score += 3
        signals.append("📈 量縮後放量回升")

    # 加碼訊號3：MA5 在 MA20 上方（短期趨勢健康）
    if ma5 > ma20:
        score += 1
        signals.append("✅ MA5 > MA20（趨勢向上）")

    # 加碼訊號4：今日收紅
    if today_close > today["open"]:
        score += 1
        signals.append("🕯️ 今日收紅")

    if score >= 5:
        status = "💰 加碼參考訊號"
    elif score >= 3:
        status = "👀 觀察中"
    else:
        status = "—"

    return {
        "score": score,
        "signals": signals,
        "status": status,
        "close": today_close,
        "vol_ratio": today_vol / vol_ma20 if vol_ma20 > 0 else 0,
        "ma20": ma20,
    }


def fetch_name_map(codes: list) -> dict:
    """抓股票名稱，優先從 watchlist.json 取"""
    name_map = {}

    # 先從 watchlist.json 取名稱
    wl_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "watchlist.json")
    try:
        with open(wl_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        for h in data.get("holdings", []):
            name_map[h["code"]] = h["name"]
        for s in data.get("watchlist", []):
            name_map[s["code"]] = s["name"]
    except: pass

    # 補上常見名稱
    defaults = {
        "2330": "台積電", "2317": "鴻海", "2303": "聯電", "3006": "晶豪科",
        "3034": "聯詠", "3545": "敦泰", "8016": "矽創", "4961": "天鈺",
        "2454": "聯發科", "6196": "帆宣", "3037": "欣興", "8046": "南電",
        "3189": "景碩", "2049": "上銀", "2634": "漢翔", "3005": "神基",
        "2376": "技嘉", "3231": "緯創", "1326": "台化", "1301": "台塑",
        "2327": "國巨", "2308": "台達電", "2054": "東和鋼鐵", "2002": "中鋼",
        "2027": "大成鋼", "2014": "中鴻", "1504": "東元", "1513": "中興電",
        "1503": "士電", "9941": "裕融", "5871": "中租-KY", "8436": "大江",
        "1707": "葡萄王", "2379": "瑞昱", "3558": "神準", "2618": "長榮航",
        "2610": "華航", "2603": "長榮", "2385": "群光", "1477": "聚陽",
        "4164": "承業醫", "1720": "生達", "1752": "南光", "4114": "健喬",
        "1216": "統一", "2357": "華碩", "2412": "中華電",
        "006208": "富邦台50", "00878": "國泰高股息",
    }
    for k, v in defaults.items():
        if k not in name_map:
            name_map[k] = v
    return name_map


def format_holding_section(results: list) -> str:
    lines = ["📊 <b>持倉監控</b>"]
    for r in results:
        code  = r["code"]
        name  = r["name"]
        score = r["score"]
        status= r["status"]
        close = r.get("close", 0)
        stars = "⭐" * min(score // 2, 5) if score >= 4 else ""

        if r.get("is_etf"):
            lines.append(f"\n<b>{code} {name}</b>（ETF）")
        else:
            lines.append(f"\n<b>{code} {name}</b>")

        lines.append(f"  收盤 {close:.1f}｜{status} {stars}")
        for sig in r["signals"]:
            lines.append(f"  {sig}")
        if not r["signals"]:
            lines.append("  量價正常，無特殊訊號")
    return "\n".join(lines)


def format_watchlist_section(results: list) -> str:
    # 只顯示有訊號的（score >= 3）
    triggered = [r for r in results if r["score"] >= 3]
    if not triggered:
        return "🔍 <b>自選股</b>\n  今日無洗籌碼/突破訊號"

    lines = [f"🎯 <b>自選股訊號（{len(triggered)} 檔）</b>"]
    for i, r in enumerate(triggered, 1):
        score = r["score"]
        stars = "⭐" * min(score // 2, 5)
        lines.append(f"\n{i}. <b>{r['code']} {r['name']}</b> {stars}")
        lines.append(f"   收盤 {r.get('close',0):.1f}｜{r['status']}")
        for sig in r["signals"]:
            lines.append(f"   {sig}")
    return "\n".join(lines)


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 洗籌碼雷達啟動...")

    # 取得股票名稱
    print("取得股票名稱...")
    all_codes = HOLDINGS + WATCHLIST
    name_map = fetch_name_map(all_codes)

    # 處理持倉
    print(f"\n分析持倉（{len(HOLDINGS)} 支）...")
    holding_results = []
    for code in HOLDINGS:
        print(f"  {code} {name_map.get(code, '')}...")
        history = fetch_history(code, days=25)
        if not history:
            print(f"    無資料")
            continue
        is_etf = code in ETF_CODES
        if is_etf:
            r = analyze_etf(code, history)
        else:
            r = analyze_stock(code, history)
        r["code"]   = code
        r["name"]   = name_map.get(code, code)
        r["is_etf"] = is_etf
        holding_results.append(r)
        time.sleep(0.3)

    # 處理自選股
    print(f"\n分析自選股（{len(WATCHLIST)} 支）...")
    watchlist_results = []
    for code in WATCHLIST:
        print(f"  {code} {name_map.get(code, '')}...", end=" ")
        history = fetch_history(code, days=25)
        if not history:
            print("無資料")
            continue
        r = analyze_stock(code, history)
        r["code"] = code
        r["name"] = name_map.get(code, code)
        watchlist_results.append(r)
        print(f"score={r['score']} {r['status']}")
        time.sleep(0.3)

    # 自選股依分數排序
    watchlist_results.sort(key=lambda x: x["score"], reverse=True)

    # 組訊息
    now_str = datetime.now().strftime("%m/%d %H:%M")
    header  = f"🎯 <b>洗籌碼雷達｜{now_str}</b>\n{'─'*20}"
    holding_msg   = format_holding_section(holding_results)
    watchlist_msg = format_watchlist_section(watchlist_results)
    footer  = "\n─────────────────────\n⚠️ 僅供參考，請自行判斷"

    full_msg = f"{header}\n\n{holding_msg}\n\n{watchlist_msg}{footer}"

    print("\n推送 Telegram...")
    ok = send_telegram(full_msg)
    print(f"Telegram：{'成功' if ok else '失敗'}")

    # 儲存 JSON 供 GitHub Pages 使用
    all_results = []
    for r in holding_results + watchlist_results:
        all_results.append({
            "code":    r.get("code", ""),
            "name":    r.get("name", ""),
            "score":   r.get("score", 0),
            "status":  r.get("status", ""),
            "close":   r.get("close", 0),
            "signals": r.get("signals", []),
            "vol_ratio": round(r.get("vol_ratio", 0), 2),
            "ma20":    round(r.get("ma20", 0), 2),
            "is_etf":  r.get("is_etf", False),
        })

    out = {
        "generated": datetime.now().strftime("%Y/%m/%d %H:%M"),
        "results": all_results
    }
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "wash_radar.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"✅ JSON 已存到：{json_path}")
    print("   請上傳到 GitHub: data/wash_radar.json")


if __name__ == "__main__":
    main()
