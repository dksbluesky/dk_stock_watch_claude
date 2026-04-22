"""
holding_radar.py - dk edition
"""
import requests
import urllib3
import json
import os
from datetime import datetime, timedelta

urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8225398265:AAF8uJObOAfElE789AQPu6p7v6Y7XzbGFjk")
CHAT_ID        = os.environ.get("CHAT_ID", "8695864227")

HOLDINGS = [
    {"code": "2330",   "name": "TSMC",     "is_etf": False, "stop_loss": 1700, "dca": None},
    {"code": "006208", "name": "FTW50",    "is_etf": True,  "stop_loss": None, "dca": None},
    {"code": "00878",  "name": "CTBC-Div", "is_etf": True,  "stop_loss": None, "dca": {"amount": 40000, "day": 24}},
]

HISTORY_FILE = "data/holding_history.json"
OUTPUT_FILE  = "data/holding_radar.json"
HEADERS = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36"}


def send_telegram(text):
    url = "https://api.telegram.org/bot" + TELEGRAM_TOKEN + "/sendMessage"
    try:
        r = requests.post(url, json={"chat_id": CHAT_ID, "text": text, "parse_mode": "HTML"}, timeout=10, verify=False)
        return r.json().get("ok", False)
    except Exception as e:
        print("Telegram failed: " + str(e))
        return False


def get_trading_date():
    d = datetime.now()
    if d.hour < 15:
        d -= timedelta(days=1)
    while d.weekday() >= 5:
        d -= timedelta(days=1)
    return d.strftime("%Y%m%d")


def fetch_institutional(code, date):
    url = "https://www.twse.com.tw/rwd/zh/fund/T86?response=json&date=" + date + "&selectType=ALL"
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, verify=False)
        if r.status_code != 200:
            return {}
        data = r.json()
        if data.get("stat") != "OK":
            return {}
        for row in data.get("data", []):
            if len(row) < 15:
                continue
            if str(row[0]).strip() == code:
                def st(v):
                    try: return int(str(v).replace(",", "").strip()) // 1000
                    except: return 0
                return {
                    "foreign_buy":  st(row[2]),
                    "foreign_sell": st(row[3]),
                    "foreign_net":  st(row[4]),
                    "trust_net":    st(row[8]),
                    "dealer_net":   st(row[11]),
                    "total_net":    st(row[13]),
                }
    except Exception as e:
        print("Institutional failed: " + str(e))
    return {}


def fetch_broker(code, date):
    url = "https://www.twse.com.tw/rwd/zh/fund/TWT38U?response=json&date=" + date + "&stockNo=" + code
    try:
        r = requests.get(url, headers=HEADERS, timeout=20, verify=False)
        if r.status_code != 200:
            return {}
        data = r.json()
        if data.get("stat") != "OK":
            return {}
        for row in data.get("data", []):
            if len(row) < 6:
                continue
            if str(row[1]).strip() != code:
                continue
            def st(v):
                try: return int(str(v).replace(",", "").strip()) // 1000
                except: return 0
            return {
                "broker_buy":   st(row[3]),
                "broker_sell":  st(row[4]),
                "broker_net":   st(row[5]),
                "buy_brokers":  0,
                "sell_brokers": 0,
                "broker_diff":  0,
            }
    except Exception as e:
        print("Broker failed: " + str(e))
    return {}


def fetch_price_and_volume(code):
    for suffix in [".TW", ".TWO"]:
        try:
            r = requests.get(
                "https://query1.finance.yahoo.com/v8/finance/chart/" + code + suffix + "?interval=1d&range=5d",
                headers=HEADERS, timeout=15, verify=False
            )
            result = r.json().get("chart", {}).get("result", [])
            if result:
                quotes = result[0].get("indicators", {}).get("quote", [{}])[0]
                closes = [c for c in quotes.get("close", []) if c]
                vols   = [v for v in quotes.get("volume", []) if v]
                if closes and vols:
                    return closes[-1], int(vols[-1] / 1000)
        except:
            continue
    return None, 0


def load_history():
    if os.path.exists(HISTORY_FILE):
        try:
            with open(HISTORY_FILE, "r", encoding="utf-8") as f:
                return json.load(f)
        except:
            pass
    return {}


def save_history(history):
    os.makedirs("data", exist_ok=True)
    with open(HISTORY_FILE, "w", encoding="utf-8") as f:
        json.dump(history, f, ensure_ascii=False, indent=2)


def calc_concentration(history_list, days):
    recent = history_list[-days:] if len(history_list) >= days else history_list
    if not recent:
        return 0.0
    net_sum = sum(d.get("foreign_net", 0) for d in recent)
    vol_sum = sum(d.get("volume", 0) for d in recent)
    if vol_sum == 0:
        return 0.0
    return round(net_sum / vol_sum * 100, 2)


def check_dca_reminder(holdings_config):
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
        if today.day not in [day - 1, day]:
            continue
        price, _ = fetch_price_and_volume(code)
        day_label = "Tomorrow" if today.day == day - 1 else "Today"
        if price:
            shares    = int(amount / price / 1000)
            price_str = str(round(price, 2)) + " (~" + str(shares) + " lots)"
        else:
            price_str = "N/A"
        msg = ("<b>DCA Reminder: " + code + " " + name + "</b>\n"
               + day_label + " (" + str(today.month) + "/" + str(day) + ") NT$" + "{:,}".format(amount) + "\n"
               + "Price: " + price_str)
        alerts.append(msg)
    if alerts:
        send_telegram("\n\n".join(alerts))
        print("DCA reminder sent: " + str(len(alerts)))


def check_stop_loss(holdings_config):
    alerts = []
    for h in holdings_config:
        sl = h.get("stop_loss")
        if not sl:
            continue
        code = h["code"]
        name = h["name"]
        price, _ = fetch_price_and_volume(code)
        if price and price <= sl:
            msg = ("<b>STOP LOSS ALERT: " + code + " " + name + "</b>\n"
                   + "Price " + str(round(price, 1)) + " &lt;= Stop " + str(sl) + "\n"
                   + "Please check immediately!")
            alerts.append(msg)
            print("STOP LOSS HIT: " + code + " price=" + str(round(price, 1)))
        elif price:
            pct = (price - sl) / sl * 100
            print(code + " price=" + str(round(price, 1)) + " margin=" + str(round(pct, 1)) + "%")
    if alerts:
        send_telegram("\n\n".join(alerts))


def analyze_holding(code, name, is_etf, history):
    date = get_trading_date()
    print("Fetching " + code + " (" + date + ")...")

    inst           = fetch_institutional(code, date)
    broker         = fetch_broker(code, date)
    price, vol     = fetch_price_and_volume(code)

    today_data = {}
    today_data.update(inst)
    today_data.update(broker)
    today_data["volume"] = vol
    today_data["date"]   = date

    h_list = history.get(code, [])
    if not any(d["date"] == date for d in h_list):
        h_list.append(today_data)
    h_list = sorted(h_list, key=lambda x: x["date"])[-30:]
    history[code] = h_list

    foreign_net = today_data.get("foreign_net", 0)
    total_net   = today_data.get("total_net", 0)
    broker_net  = today_data.get("broker_net", 0)
    broker_diff = today_data.get("broker_diff", 0)
    conc_5d     = calc_concentration(h_list, 5)
    conc_20d    = calc_concentration(h_list, 20)

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
        "conc5_rising":    len(h_list) >= 2 and conc_5d > calc_concentration(h_list[:-1], 5),
        "conc20_positive": conc_20d > 0,
        "broker_diff_neg": broker_diff < 0,
        "price_support":   vol > 0,
    }
    exhaustion = sum(signals.values())

    return {
        "code":        code,
        "name":        name,
        "is_etf":      is_etf,
        "date":        date,
        "close":       round(price, 1) if price else 0,
        "foreign_net": foreign_net,
        "total_net":   total_net,
        "broker_net":  broker_net,
        "broker_diff": broker_diff,
        "conc_5d":     conc_5d,
        "conc_20d":    conc_20d,
        "streak":      streak,
        "signals":     signals,
        "exhaustion":  exhaustion,
        "history_5d":  [{"date": d["date"], "foreign_net": d.get("foreign_net", 0)} for d in h_list[-5:]],
    }


def fmt(v):
    if v is None:
        return "--"
    return ("+" if v > 0 else "") + "{:,}".format(v)


def arrow(v):
    if v is None or v == 0:
        return "~"
    return "+" if v > 0 else "-"


def format_telegram(results, date):
    lines = ["<b>Holdings Radar | " + date[:4] + "/" + date[4:6] + "/" + date[6:] + "</b>"]
    for r in results:
        code   = r["code"]
        name   = r["name"]
        fn     = r["foreign_net"]
        tn     = r["total_net"]
        c5     = r["conc_5d"]
        c20    = r["conc_20d"]
        bd     = r["broker_diff"]
        streak = r["streak"]
        exh    = r["exhaustion"]
        sigs   = r["signals"]
        hist5  = r.get("history_5d", [])
        close  = r.get("close", 0)

        lines.append("\n" + "-"*20)
        lines.append("<b>" + code + " " + name + "</b>  Close: " + str(close))

        def ok(k):
            return "Y" if sigs.get(k) else "N"

        if r["is_etf"]:
            lines.append("(ETF - no wash/institutional logic)")
            lines.append("Foreign: " + arrow(fn) + " " + fmt(fn) + " lots")
            lines.append("Institutional: " + arrow(tn) + " " + fmt(tn) + " lots")
            lines.append("5D conc: " + str(c5) + "%")
        else:
            lines.append("1 Foreign net:   " + arrow(fn) + " " + fmt(fn) + " [" + ok("foreign_flip") + "]")
            lines.append("2 Institutional: " + arrow(tn) + " " + fmt(tn) + " [" + ok("total_positive") + "]")
            lines.append("3 5D conc:  " + str(c5) + "% [" + ok("conc5_rising") + "]")
            lines.append("4 20D conc: " + str(c20) + "% [" + ok("conc20_positive") + "]")
            lines.append("5 Broker diff: " + fmt(bd) + " [" + ok("broker_diff_neg") + "]")
            lines.append("6 Support: [" + ok("price_support") + "]")

            if streak < 0:
                streak_str = "Selling " + str(abs(streak)) + "d"
            elif streak > 0:
                streak_str = "Buying " + str(streak) + "d"
            else:
                streak_str = "Flipped"
            lines.append("Foreign trend: " + streak_str)

            if exh >= 5:
                lines.append("*** EXHAUSTION: " + str(exh) + "/6 CONFIRMED ***")
            elif exh >= 3:
                lines.append("** Exhaustion: " + str(exh) + "/6 Watch **")
            else:
                lines.append("Exhaustion: " + str(exh) + "/6")

            if len(hist5) >= 2:
                trend = " > ".join([fmt(d["foreign_net"]) for d in hist5])
                lines.append("Recent: " + trend)

    lines.append("\n" + "-"*20 + "\nFor reference only.")
    return "\n".join(lines)


def main():
    print("[" + datetime.now().strftime("%Y-%m-%d %H:%M") + "] Holdings Radar starting...")
    date = get_trading_date()
    print("Trading date: " + date)

    print("Checking stop loss...")
    check_stop_loss(HOLDINGS)
    check_dca_reminder(HOLDINGS)

    history = load_history()

    results = []
    for h in HOLDINGS:
        r = analyze_holding(h["code"], h["name"], h["is_etf"], history)
        results.append(r)
        print(h["code"] + ": foreign=" + str(r["foreign_net"]) + " exhaustion=" + str(r["exhaustion"]) + "/6")

    save_history(history)
    print("History saved")

    out = {
        "generated": datetime.now().strftime("%Y/%m/%d %H:%M"),
        "date":      date,
        "holdings":  results,
    }
    os.makedirs("data", exist_ok=True)
    with open(OUTPUT_FILE, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print("JSON saved to " + OUTPUT_FILE)

    msg = format_telegram(results, date)
    ok  = send_telegram(msg)
    print("Telegram: " + ("OK" if ok else "FAILED"))


if __name__ == "__main__":
    main()
