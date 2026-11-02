"""抓取一週的 S&P 500 成分股財報時間表，輸出 data/earnings.json。

資料來源：
- S&P 500 成分股與 GICS 產業：Wikipedia「List of S&P 500 companies」
- 財報日期與盤前/盤後：Nasdaq 財報行事曆 API（api.nasdaq.com，免金鑰）

用法：python scripts/fetch_earnings.py --week current|next
  current：本週（美東週六、週日執行時視為下一週）
  next：下一週（週六晨報用）
"""
import argparse
import datetime as dt
import io
import json
import os
import re
import sys
from zoneinfo import ZoneInfo

import pandas as pd
import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
ET = ZoneInfo("America/New_York")
TW = ZoneInfo("Asia/Taipei")
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}
WEEKDAY_ZH = "一二三四五六日"

SECTOR_ZH = {
    "Information Technology": "資訊科技",
    "Communication Services": "通訊服務",
    "Consumer Discretionary": "非必需消費",
    "Consumer Staples": "必需消費",
    "Health Care": "醫療保健",
    "Financials": "金融",
    "Industrials": "工業",
    "Energy": "能源",
    "Materials": "原物料",
    "Real Estate": "房地產",
    "Utilities": "公用事業",
}
TECH_SECTORS = {"Information Technology", "Communication Services"}


def norm(sym):
    return re.sub(r"[./]", "-", str(sym).strip().upper())


def sp500():
    url = "https://en.wikipedia.org/wiki/List_of_S%26P_500_companies"
    html = requests.get(url, headers=UA, timeout=30).text
    t = pd.read_html(io.StringIO(html), attrs={"id": "constituents"})[0]
    out = {}
    for _, r in t.iterrows():
        sector = r["GICS Sector"]
        sub = str(r.get("GICS Sub-Industry", ""))
        out[norm(r["Symbol"])] = {
            "symbol": str(r["Symbol"]).replace(".", "-"),
            "name": r["Security"],
            "sector": sector,
            "sector_zh": SECTOR_ZH.get(sector, sector),
            "sub_industry": sub,
            "is_tech": sector in TECH_SECTORS,
            "is_semi": "Semiconductor" in sub,
        }
    return out


def nasdaq_day(d):
    url = f"https://api.nasdaq.com/api/calendar/earnings?date={d.isoformat()}"
    headers = {
        **UA,
        "Accept": "application/json, text/plain, */*",
        "Origin": "https://www.nasdaq.com",
        "Referer": "https://www.nasdaq.com/",
    }
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    data = r.json().get("data") or {}
    return data.get("rows") or []


def mcap(v):
    try:
        return float(re.sub(r"[^\d.]", "", str(v)) or 0)
    except ValueError:
        return 0.0


def tw_window(d, h1, m1, h2, m2):
    a = dt.datetime(d.year, d.month, d.day, h1, m1, tzinfo=ET).astimezone(TW)
    b = dt.datetime(d.year, d.month, d.day, h2, m2, tzinfo=ET).astimezone(TW)
    return f"{a:%m/%d %H:%M}–{b:%H:%M}"


def week_monday(mode):
    today = dt.datetime.now(ET).date()
    monday = today - dt.timedelta(days=today.weekday())
    if mode == "current" and today.weekday() >= 5:
        monday += dt.timedelta(days=7)
    if mode == "next":
        monday += dt.timedelta(days=7)
    return today, monday


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--week", choices=["current", "next"], default="current")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "earnings.json"))
    args = ap.parse_args()

    today_et, monday = week_monday(args.week)
    now_et = dt.datetime.now(ET)
    members = sp500()

    def reported(d, key):
        """盤前在美東 09:30 後、盤後在美東 17:30 後視為已公布；時間未定的要等到隔天。"""
        cutoff = {"bmo": (9, 30), "amc": (17, 30), "tbd": (23, 59)}[key]
        return now_et >= dt.datetime(d.year, d.month, d.day, *cutoff, tzinfo=ET)
    days, errors, total = [], {}, 0
    for i in range(5):
        d = monday + dt.timedelta(days=i)
        groups = {"bmo": [], "amc": [], "tbd": []}
        try:
            rows = nasdaq_day(d)
        except Exception as e:  # noqa: BLE001
            errors[d.isoformat()] = f"{type(e).__name__}: {e}"
            rows = []
        for r in rows:
            info = members.get(norm(r.get("symbol")))
            if not info:
                continue
            t = str(r.get("time", ""))
            key = "bmo" if "pre-market" in t else "amc" if "after-hours" in t else "tbd"
            groups[key].append(
                {
                    **info,
                    "market_cap": mcap(r.get("marketCap")),
                    "eps_forecast": r.get("epsForecast") or None,
                    "fiscal_quarter": r.get("fiscalQuarterEnding") or None,
                    "reported": reported(d, key),
                    "logo": f"https://financialmodelingprep.com/image-stock/{info['symbol']}.png",
                }
            )
        for k in groups:
            groups[k].sort(key=lambda x: x["market_cap"], reverse=True)
        n = sum(len(v) for v in groups.values())
        total += n
        days.append(
            {
                "date": d.isoformat(),
                "label": f"{d.month}/{d.day}（{WEEKDAY_ZH[d.weekday()]}）",
                "bmo_tw": tw_window(d, 7, 0, 9, 30),
                "amc_tw": tw_window(d, 16, 0, 17, 30),
                "count": n,
                **groups,
            }
        )

    friday = monday + dt.timedelta(days=4)
    result = {
        "week_start": monday.isoformat(),
        "week_end": friday.isoformat(),
        "title": f"{monday.month}/{monday.day}–{friday.month}/{friday.day}",
        "total": total,
        "tech_total": sum(1 for d in days for k in ("bmo", "amc", "tbd") for c in d[k] if c["is_tech"]),
        "days": days,
        "errors": errors,
        "source": "Nasdaq 財報行事曆、Wikipedia S&P 500 成分股",
    }
    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    print(f"earnings.json 已寫入：{result['title']} 共 {total} 家（科技 {result['tech_total']}），錯誤 {list(errors)}")
    if errors:
        print(json.dumps(errors, ensure_ascii=False), file=sys.stderr)


if __name__ == "__main__":
    main()
