"""
飆股雷達 - 月營收監控 + Telegram 推送
直接抓 MOPS 靜態 HTML 檔案，不需要 Playwright
"""

import re
import json
import os
import requests
import urllib3
import re
import json
import os
from datetime import datetime, timedelta
urllib3.disable_warnings(urllib3.exceptions.InsecureRequestWarning)

# ============================================================
# 設定區
# ============================================================
TELEGRAM_TOKEN = os.environ.get("TELEGRAM_TOKEN", "8225398265:AAF8uJObOAfElE789AQPu6p7v6Y7XzbGFjk")
CHAT_ID        = os.environ.get("CHAT_ID", "8695864227")
YOY_THRESHOLD  = 20.0   # 去年同月增減(%) 門檻（下限）
YOY_MAX        = 500.0  # YoY 上限：超過通常是建案認列，非真實成長
REV_LY_MIN     = 10000  # 去年同月營收下限（千元）= 1000 萬，排除基期近零
TOP_N          = 15      # 最多推送幾檔

# 排除產業關鍵字（建設/不動產類股營收認列時間不固定，YoY 失真）
EXCLUDE_INDUSTRIES = [
    "建設", "建築", "不動產", "地產", "開發", "營建",
    "住宅", "商辦", "豪宅", "置地",
]
# ============================================================

# 靜態 HTML URL 規律：
# 上市(sii)：https://mopsov.twse.com.tw/nas/t21/sii/t21sc03_{year}_{month}_0.html
# 上櫃(otc)：https://mopsov.twse.com.tw/nas/t21/otc/t21sc03_{year}_{month}_0.html
BASE_URLS = [
    "https://mopsov.twse.com.tw/nas/t21/sii/t21sc03_{year}_{month}_0.html",  # 上市
    "https://mopsov.twse.com.tw/nas/t21/otc/t21sc03_{year}_{month}_0.html",  # 上櫃
]

HEADERS = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 Chrome/120.0.0.0 Safari/537.36",
    "Referer": "https://mops.twse.com.tw/",
}


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


def get_target_ym():
    """10日前抓上上月，10日後抓上月"""
    now = datetime.now()
    if now.day <= 10:
        t = (now.replace(day=1) - timedelta(days=1)).replace(day=1) - timedelta(days=1)
    else:
        t = now.replace(day=1) - timedelta(days=1)
    return t.year - 1911, t.month  # 民國年, 月


def fetch_html(year_roc: int, month: int) -> list:
    """抓上市 + 上櫃的靜態 HTML"""
    all_html = []
    for url_template in BASE_URLS:
        url = url_template.format(year=year_roc, month=month)
        market = "上市" if "sii" in url else "上櫃"
        try:
            r = requests.get(url, headers=HEADERS, timeout=20, verify=False)
            r.encoding = "big5"
            print(f"  {market}: HTTP {r.status_code}, 長度 {len(r.text)}")
            if r.status_code == 200 and len(r.text) > 1000:
                all_html.append((market, r.text))
            else:
                print(f"  {market}: 資料不足，可能尚未公布")
        except Exception as e:
            print(f"  {market} 抓取失敗: {e}")
    return all_html


def parse_html(html: str, market: str) -> list:
    """解析 HTML 表格，提取月營收資料"""
    results = []

    # 找所有 <tr> 行
    rows = re.findall(r'<tr[^>]*>(.*?)</tr>', html, re.DOTALL | re.IGNORECASE)

    for row_html in rows:
        # 提取每格，去掉 HTML 標籤
        cells = re.findall(r'<t[dh][^>]*>(.*?)</t[dh]>', row_html, re.DOTALL | re.IGNORECASE)
        cells = [re.sub(r'<[^>]+>', '', c).replace('\xa0', '').replace(',', '').strip() for c in cells]
        cells = [c for c in cells if c != '']

        if len(cells) < 7:
            continue

        code = cells[0].strip()
        # 只處理 4 碼股票代號
        if not re.match(r'^\d{4}$', code):
            continue

        try:
            name   = cells[1]
            rev_m  = float(cells[2]) if cells[2].lstrip('-').replace('.','').isdigit() else 0
            rev_ly = float(cells[4]) if len(cells) > 4 and cells[4].lstrip('-').replace('.','').isdigit() else 0
            # 欄位6：去年同月增減(%)
            yoy_s  = cells[6] if len(cells) > 6 else "0"
            yoy    = float(yoy_s) if re.match(r'^-?\d+\.?\d*$', yoy_s) else 0

            if rev_m == 0:
                continue

            # 若沒有 YoY 欄位，自行計算
            if yoy == 0 and rev_ly > 0:
                yoy = (rev_m - rev_ly) / rev_ly * 100

            results.append({
                "code": code,
                "name": name,
                "market": market,
                "rev_m": rev_m,    # 單位：千元
                "rev_ly": rev_ly,
                "yoy": yoy,
            })
        except:
            continue

    return results


def format_message(stocks: list, year_roc: int, month: int) -> str:
    year_ad = year_roc + 1911
    lines = [
        f"🚀 <b>飆股雷達｜{year_ad}年{month}月營收</b>",
        f"篩選：YoY > {YOY_THRESHOLD:.0f}%（上市+上櫃），共 {len(stocks)} 檔",
        f"更新：{datetime.now().strftime('%m/%d %H:%M')}",
        "─────────────────────",
    ]
    for i, s in enumerate(stocks, 1):
        icon = "🔴" if s["yoy"] >= 50 else "🟠" if s["yoy"] >= 30 else "🟡"
        rev_b = s["rev_m"] / 100000  # 千元 → 億
        lines.append(
            f"{i}. <b>{s['code']} {s['name']}</b> [{s['market']}]\n"
            f"   月營收 {rev_b:.2f}億｜YoY {icon} <b>+{s['yoy']:.1f}%</b>"
        )
    lines += ["─────────────────────", "⚠️ 僅供參考，請自行做功課"]
    return "\n".join(lines)


def main():
    print(f"[{datetime.now().strftime('%Y-%m-%d %H:%M')}] 飆股雷達啟動...")

    year_roc, month = get_target_ym()
    print(f"目標月份：民國 {year_roc} 年 {month} 月")
    print(f"抓取中...")

    html_list = fetch_html(year_roc, month)

    if not html_list:
        send_telegram(f"⚠️ 飆股雷達：{year_roc+1911}年{month}月資料尚未公布")
        return

    # 解析所有市場
    all_stocks = []
    for market, html in html_list:
        stocks = parse_html(html, market)
        print(f"  {market} 解析到 {len(stocks)} 筆")
        all_stocks.extend(stocks)

    print(f"合計 {len(all_stocks)} 筆")

    if not all_stocks:
        send_telegram(f"⚠️ 飆股雷達：資料解析失敗")
        return

    # 篩選並排序
    def is_valid(s):
        # 條件1：YoY 在合理範圍內
        if not (YOY_THRESHOLD <= s["yoy"] <= YOY_MAX):
            return False
        # 條件2：去年同月營收有基期（排除基期近零的建案認列）
        if s["rev_ly"] < REV_LY_MIN:
            return False
        # 條件3：排除建設/不動產類股
        if any(kw in s["name"] for kw in EXCLUDE_INDUSTRIES):
            return False
        return True

    filtered = sorted(
        [s for s in all_stocks if is_valid(s)],
        key=lambda x: x["yoy"], reverse=True
    )[:TOP_N]

    print(f"YoY > {YOY_THRESHOLD}% 共 {len(filtered)} 檔")

    if filtered:
        ok = send_telegram(format_message(filtered, year_roc, month))
        print(f"Telegram：{'成功' if ok else '失敗'}")
    else:
        send_telegram(f"📭 {year_roc+1911}年{month}月無 YoY > {YOY_THRESHOLD:.0f}% 標的")

    # 儲存 JSON 供 GitHub Pages 使用（存所有符合條件的，不限 TOP_N）
    all_filtered = sorted(
        [s for s in all_stocks if is_valid(s)],
        key=lambda x: x["yoy"], reverse=True
    )
    out = {
        "generated": datetime.now().strftime("%Y/%m/%d %H:%M"),
        "year": year_roc + 1911,
        "month": month,
        "stocks": all_filtered
    }
    json_path = os.path.join(os.path.dirname(os.path.abspath(__file__)), "revenue.json")
    with open(json_path, "w", encoding="utf-8") as f:
        json.dump(out, f, ensure_ascii=False, indent=2)
    print(f"✅ JSON 已存到：{json_path}")
    print("   請上傳到 GitHub: data/revenue.json")


if __name__ == "__main__":
    main()
