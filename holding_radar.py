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

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# 設定區
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8225398265:AAF8uJObOAfElE789AQPu6p7v6Y7XzbGFjk")
CHAT_ID        = os.environ.get("CHAT_ID", "8695864227")

# 持倉清單（可新增/刪除）
HOLDINGS = [
    {"code": "2330",   "name": "台積電",     "is_etf": False, "stop_loss": 1700, "dca": None},
    {"code": "006208", "name": "富邦台50",   "is_etf": True,  "stop_loss": None, "dca": None},
    {"code": "00878",  "name": "國泰高股息", "is_etf": True,  "stop_loss": None,
     "dca": {"amount": 40000, "day": 6}},  # 每月6日扣款 40,000元
]

HISTORY_FILE = "data/holding_history.json"  # 累積歷史資料
OUTPUT_FILE  = "data/holding_radar.json"    # 網頁用

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
    """取得最近交易日（週末往前找）"""
    d = datetime.now()
    # 收盤前用前一個交易日
    if d.hour < 15:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def fetch_institutional(code: str, date: str) -> dict:
    """
    抓三大法人買賣超（T86）
    欄位：外資買進/賣出/買超, 投信買進/賣出/買超, 自營商, 三大合計
    """
    url = f"https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date={date}&selectType=ALL"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, verify=False)
        if r.status_code != 200:
            return {}
        data = r.json()
        if data.get("stat") != "OK":
            return {}
        rows = data.get("data", [])
        for row in rows:
            if len(row) < 15: continue
            if str(row[0]).strip() == code:
                def sf(v):
                    try: return int(str(v).replace(",","").strip())
                    except: return 0
                def st(v):  # 股→張
                    try: return int(str(v).replace(",","").strip()) // 1000
                    except: return 0
                return {
                    "foreign_buy":    st(row[2]),
                    "foreign_sell":   st(row[3]),
                    "foreign_net":    st(row[4]),   # 外資買賣超（張）
                    "trust_buy":      st(row[6]),
                    "trust_sell":     st(row[7]),
                    "trust_net":      st(row[8]),   # 投信買賣超
                    "dealer_net":     st(row[11]),  # 自營商買賣超
                    "total_net":      st(row[13]),  # 三大法人合計
                }
    except Exception as e:
        print(f"  三大法人抓取失敗: {e}")
    return {}


def fetch_broker(code: str, date: str) -> dict:
    """
    TWT38U 回傳全市場外資買賣資料，找到指定代號那行
    欄位：[0]空白 [1]代號 [2]名稱 [3]買進股數 [4]賣出股數 [5]買賣超股數 ...
    家數差目前無法從此 API 取得，暫時省略
    """
    url = f"https://www.twse.com.tw/rwd/zh/fund/TWT38U?response=json&date={date}&stockNo={code}"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, verify=False)
        if r.status_code != 200:
            return {}
        data = r.json()
        if data.get("stat") != "OK":
            return {}
        rows = data.get("data", [])
        for row in rows:
            if len(row) < 6: continue
            row_code = str(row[1]).strip()
            if row_code != code: continue
            def st(v):
                try: return int(str(v).replace(",","").strip()) // 1000
                except: return 0
            buy = st(row[3])
            sell= st(row[4])
            net = st(row[5])
            return {
                "broker_buy":  buy,
                "broker_sell": sell,
                "broker_net":  net,
                "buy_brokers": 0,   # 需要分點明細，此API無法計算
                "sell_brokers":0,
                "broker_diff": 0,
            }
    except Exception as e:
        print(f"  分點資料抓取失敗: {e}")
    return {}


def fetch_volume(code: str, date: str) -> int:
    """抓當日總成交量（張）"""
    for suffix in [".TW", ".TWO"]:
        try:
            r = requests.get(
                f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{suffix}?interval=1d&range=5d",
                headers=HEADERS, timeout=15, verify=False
            )
            data = r.json()
            result = data.get("chart", {}).get("result", [])
            if result:
                vols = result[0].get("indicators", {}).get("quote", [{}])[0].get("volume", [])
                vols = [v for v in vols if v]
                if vols:
                    return int(vols[-1] / 1000)  # 股 → 張
        except: continue
    return 0


def load_history() -> dict:
    """載入歷史資料"""
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except: pass
    return {}


def save_history(history: dict):
    """儲存歷史資料"""
    os.makedirs("data", exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def update_history(history: dict, code: str, date: str, day_data: dict) -> dict:
    """更新某支股票的歷史資料（保留最近30天）"""
    if code not in history:
        history[code] = []

    # 避免重複
    existing_dates = [d["date"] for d in history[code]]
    if date not in existing_dates:
        history[code].append({"date": date, **day_data})

    # 只保留最近30天
    history[code] = sorted(history[code], key=lambda x: x["date"])[-30:]
    return history


def calc_concentration(history_list: list, days: int) -> float:
    """
    計算集中度（%）
    = 近N日累計外資買賣超(張) / 近N日總成交量(張) * 100
    """
    recent = history_list[-days:] if len(history_list) >= days else history_list
    if not recent:
        return 0.0
    net_sum = sum(d.get("foreign_net", 0) for d in recent)
    vol_sum = sum(d.get("volume", 0) for d in recent)
    if vol_sum == 0:
        return 0.0
    # 兩者單位都是張，直接除
    return round(net_sum / vol_sum * 100, 2)


def analyze_holding(code: str, name: str, is_etf: bool, history: dict) -> dict:
    """分析單一持倉的籌碼狀態"""
    date = get_trading_date()
    print(f"  抓取 {code} {name} ({date})...")

    # 抓今日資料
    inst   = fetch_institutional(code, date)
    broker = fetch_broker(code, date)
    vol    = fetch_volume(code, date)

    # 合併今日資料
    today_data = {**inst, **broker, "volume": vol, "date": date}

    # 更新歷史
    h_list = history.get(code, [])
    h_list_new = h_list.copy()

    # 避免重複寫入
    if not any(d["date"] == date for d in h_list_new):
        h_list_new.append(today_data)
    h_list_new = sorted(h_list_new, key=lambda x: x["date"])[-30:]
    history[code] = h_list_new

    # 計算指標
    foreign_net   = today_data.get("foreign_net", 0)
    total_net     = today_data.get("total_net", 0)
    broker_net    = today_data.get("broker_net", 0)
    broker_diff   = today_data.get("broker_diff", 0)
    conc_5d       = calc_concentration(h_list_new, 5)
    conc_20d      = calc_concentration(h_list_new, 20)

    # 連續買超/賣超天數
    streak = 0
    for d in reversed(h_list_new):
        net = d.get("foreign_net", 0)
        if foreign_net <= 0 and net <= 0:
            streak -= 1
        elif foreign_net > 0 and net > 0:
            streak += 1
        else:
            break

    # 六大耗盡訊號評估
    signals = {
        "foreign_flip":    foreign_net > 0,                    # ①外資翻正
        "total_positive":  total_net > 0,                      # ②三大法人合計正
        "conc5_rising":    len(h_list_new) >= 2 and            # ③5日集中度上升
                           conc_5d > calc_concentration(h_list_new[:-1], 5),
        "conc20_positive": conc_20d > 0,                       # ④20日集中度轉正
        "broker_diff_neg": broker_diff < 0,                    # ⑤家數差轉負（散戶放棄）
        "price_support":   vol > 0,                            # ⑥守支撐（簡化：有成交量）
    }
    exhaustion_count = sum(signals.values())

    return {
        "code":          code,
        "name":          name,
        "is_etf":        is_etf,
        "date":          date,
        "foreign_net":   foreign_net,
        "total_net":     total_net,
        "broker_net":    broker_net,
        "broker_diff":   broker_diff,
        "conc_5d":       conc_5d,
        "conc_20d":      conc_20d,
        "streak":        streak,
        "signals":       signals,
        "exhaustion":    exhaustion_count,
        "history_5d":    [{"date": d["date"], "foreign_net": d.get("foreign_net", 0)}
                          for d in h_list_new[-5:]],
    }


def format_telegram(results: list, date: str) -> str:
    lines = [f"📊 <b>持倉籌碼分析｜{date[:4]}/{date[4:6]}/{date[6:]}</b>"]

    for r in results:
        code  = r["code"]
        name  = r["name"]
        fn    = r["foreign_net"]
        tn    = r["total_net"]
        bn    = r["broker_net"]
        bd    = r["broker_diff"]
        c5    = r["conc_5d"]
        c20   = r["conc_20d"]
        streak= r["streak"]
        exh   = r["exhaustion"]
        sigs  = r["signals"]

        def arrow(v):
            return "🟢" if v > 0 else "🔴" if v < 0 else "⚪"
        def fmt_num(v):
            return f"+{v:,}" if v > 0 else f"{v:,}"

        lines.append(f"\n{'─'*20}")
        lines.append(f"<b>{code} {name}</b>")

        if r["is_etf"]:
            lines.append("（ETF — 追蹤大盤，無主力洗盤邏輯）")
            lines.append(f"外資動向：{arrow(fn)} {fmt_num(fn)} 張")
            lines.append(f"三大法人：{arrow(tn)} {fmt_num(tn)} 張")
            lines.append(f"5日集中：{c5:+.2f}%")
        else:
            lines.append(f"① 外資買賣超：{arrow(fn)} {fmt_num(fn)} 張 {'✅' if sigs['foreign_flip'] else '❌'}")
            lines.append(f"② 三大法人：{arrow(tn)} {fmt_num(tn)} 張 {'✅' if sigs['total_positive'] else '❌'}")
            lines.append(f"③ 5日集中：{c5:+.2f}% {'✅' if sigs['conc5_rising'] else '❌'}")
            lines.append(f"④ 20日集中：{c20:+.2f}% {'✅' if sigs['conc20_positive'] else '❌'}")
            lines.append(f"⑤ 家數差：{fmt_num(bd)} {'✅' if sigs['broker_diff_neg'] else '❌'}")
            lines.append(f"⑥ 守支撐：{'✅' if sigs['price_support'] else '❌'}")

            streak_str = f"連續賣超 {abs(streak)} 天" if streak < 0 else f"連續買超 {streak} 天" if streak > 0 else "今日翻轉"
            lines.append(f"外資：{streak_str}")

            # 耗盡判斷
            if exh >= 5:
                lines.append(f"🔥 <b>賣壓耗盡訊號：{exh}/6 強烈確認</b>")
            elif exh >= 3:
                lines.append(f"⚡ 賣壓耗盡訊號：{exh}/6 觀察中")
            else:
                lines.append(f"❌ 賣壓耗盡訊號：{exh}/6 尚未確認")

            # 近5日外資趨勢
            hist5 = r.get("history_5d", [])
            if len(hist5) >= 2:
                trend = [d["foreign_net"] for d in hist5]
                trend_str = " → ".join([fmt_num(v) for v in trend])
                lines.append(f"近期外資：{trend_str}")

    lines.append(f"\n{'─'*20}")
    lines.append("⚠️ 僅供參考，請自行判斷")
    return "\n".join(lines)


def check_dca_reminder(holdings_config: list):
    """ETF 定期定額提醒（每月扣款日前一天提醒）"""
    today = datetime.now()
    alerts = []
    for h in holdings_config:
        dca = h.get("dca")
        if not dca:
            continue
        day    = dca.get("day", 1)
        amount = dca.get("amount", 0)
        code   = h["code"]
        name   = h["name"]

        # 扣款日或前一天推提醒
        if today.day not in [day - 1, day]:
            continue

        # 抓最新價格
        price = None
        for suffix in [".TW", ".TWO"]:
            try:
                r = requests.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{suffix}?interval=1d&range=5d",
                    headers=HEADERS, timeout=10, verify=False
                )
                result = r.json().get("chart", {}).get("result", [])
                if result:
                    closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
                    closes = [c for c in closes if c]
                    if closes:
                        price = closes[-1]
                        break
            except: continue

        day_label = "明天" if today.day == day - 1 else "今天"
        shares = int(amount / price / 1000) if price else 0
        price_str = f"現價 {price:.2f}，本次約買 {shares} 張" if price else "無法取得價格"
        alerts.append(
            f"📅 <b>定期定額提醒｜{code} {name}</b>
"
            f"   {day_label}（{today.month}/{day}）扣款 NT${amount:,}
"
            f"   {price_str}"
        )

    if alerts:
        send_telegram("

".join(alerts))
        print(f"DCA 提醒已推送：{len(alerts)} 筆")


def check_stop_loss(holdings_config: list):
    """檢查止損價格，若觸碰立刻推警報"""
    alerts = []
    for h in holdings_config:
        sl = h.get("stop_loss")
        if not sl:
            continue
        code = h["code"]
        name = h["name"]
        # 抓最新價格
        price = None
        for suffix in [".TW", ".TWO"]:
            try:
                r = requests.get(
                    f"https://query1.finance.yahoo.com/v8/finance/chart/{code}{suffix}?interval=1d&range=5d",
                    headers=HEADERS, timeout=10, verify=False
                )
                result = r.json().get("chart", {}).get("result", [])
                if result:
                    closes = result[0].get("indicators", {}).get("quote", [{}])[0].get("close", [])
                    closes = [c for c in closes if c]
                    if closes:
                        price = closes[-1]
                        break
            except: continue

        if price and price <= sl:
            alerts.append(
                f"🚨 <b>止損警報！{code} {name}</b>
"
                f"   現價 {price:.1f} ≤ 止損價 {sl}
"
                f"   ⚠️ 請立即確認是否執行止損！"
            )
            print(f"  ⚠️ {code} 觸碰止損！現價 {price:.1f} ≤ {sl}")
        elif price:
            pct = (price - sl) / sl * 100
            print(f"  {code} 現價 {price:.1f}，距止損 {sl} 還有 {pct:.1f}%")

    if alerts:
        send_telegram("

".join(alerts))


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 持倉籌碼雷達啟動...")
    date = get_trading_date()
    print(f"交易日：{date}")

    # 止損檢查（優先執行）
    print("檢查止損價格...")
    check_stop_loss(HOLDINGS)

    # 定期定額提醒
    check_dca_reminder(HOLDINGS)

    # 載入歷史
    history = load_history()

    # 分析每個持倉
    results = []
    for h in HOLDINGS:
        r = analyze_holding(h["code"], h["name"], h["is_etf"], history)
        results.append(r)
        print(f"  {h['code']}: 外資{r['foreign_net']:+,}張, 耗盡{r['exhaustion']}/6")

    # 儲存歷史
    save_history(history)
    print("歷史資料已更新")

    # 儲存 JSON 供網頁用
    out = {
        "generated": datetime.now().strftime("%Y/%m/%d %H:%M"),
        "date": date,
        "holdings": results
    }
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"JSON 已存到 {OUTPUT_FILE}")

    # 推送 Telegram
    msg = format_telegram(results, date)
    ok = send_telegram(msg)
    print(f"Telegram：{'成功' if ok else '失敗'}")


if __name__ == "__main__":
    main()
