"""抓取市場數據，輸出 data/market.json。

資料來源（皆免費、免金鑰）：
- yfinance：指數、ETF、個股、匯率、商品、加密貨幣
- FRED（fredgraph.csv）：美債殖利率、淨流動性（WALCL − WTREGEN − RRPONTSYD）
- CNN：恐懼貪婪指數（非官方介面）

用法：python scripts/fetch_market.py [--extra NVDA,GOOGL]
"""
import argparse
import datetime as dt
import io
import json
import math
import os
import sys
import warnings

warnings.filterwarnings("ignore")

import pandas as pd
import requests
import yfinance as yf

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
UA = {
    "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0 Safari/537.36"
}

TICKERS = {
    # 指數
    "^GSPC": "S&P 500",
    "^IXIC": "那斯達克",
    "^DJI": "道瓊",
    "^SOX": "費城半導體",
    "^RUT": "羅素 2000",
    "^VIX": "VIX",
    "^TWII": "台股加權",
    "^TNX": "美債 10 年（即時）",
    # ETF
    "SPY": "SPY / S&P 500",
    "QQQ": "QQQ / Nasdaq 100",
    "DIA": "DIA / 道瓊",
    "IWM": "IWM / 羅素 2000",
    "SOXX": "SOXX / 費半",
    # 個股
    "TSM": "台積電 ADR",
    "NVDA": "NVIDIA",
    "AAPL": "Apple",
    "MSFT": "Microsoft",
    "GOOGL": "Alphabet",
    "AMZN": "Amazon",
    "META": "Meta",
    "AVGO": "Broadcom",
    "AMD": "AMD",
    "MU": "Micron",
    "TSLA": "Tesla",
    "INTC": "Intel",
    "QCOM": "Qualcomm",
    # 匯率、商品、加密貨幣
    "DX-Y.NYB": "美元指數",
    "TWD=X": "美元/台幣",
    "JPY=X": "美元/日圓",
    "EURUSD=X": "歐元/美元",
    "CL=F": "WTI 原油",
    "BZ=F": "布蘭特原油",
    "GC=F": "黃金",
    "BTC-USD": "比特幣",
}

FG_RATING = {
    "extreme fear": "極度恐懼",
    "fear": "恐懼",
    "neutral": "中性",
    "greed": "貪婪",
    "extreme greed": "極度貪婪",
}


def _num(x, nd=4):
    if x is None:
        return None
    try:
        f = float(x)
    except (TypeError, ValueError):
        return None
    if math.isnan(f) or math.isinf(f):
        return None
    return round(f, nd)


def _pct(a, b):
    if a is None or b in (None, 0):
        return None
    return _num((a / b - 1) * 100, 2)


def _naive(series):
    idx = series.index
    if getattr(idx, "tz", None) is not None:
        series = series.copy()
        series.index = idx.tz_localize(None)
    return series


def fetch_prices(symbols):
    df = yf.download(
        symbols,
        period="4mo",
        interval="1d",
        auto_adjust=False,
        progress=False,
        group_by="ticker",
        threads=True,
    )
    out, weekly = {}, {}
    for s in symbols:
        try:
            close = df[s]["Close"].dropna() if len(symbols) > 1 else df["Close"].dropna()
        except KeyError:
            continue
        close = _naive(close)
        if len(close) < 2:
            continue
        last, prev = float(close.iloc[-1]), float(close.iloc[-2])
        last_date = close.index[-1]
        week_start = (last_date - pd.Timedelta(days=last_date.weekday())).normalize()
        before = close[close.index < week_start]
        wk_base = float(before.iloc[-1]) if len(before) else None
        out[s] = {
            "name": TICKERS.get(s, s),
            "last": _num(last),
            "prev": _num(prev),
            "date": last_date.strftime("%Y-%m-%d"),
            "chg": _num(last - prev),
            "chg_pct": _pct(last, prev),
            "wk_pct": _pct(last, wk_base),
        }
        # 期貨單日漲跌超過 8%，多半是合約換月造成的假跳動，標記提醒
        if s.endswith("=F") and out[s]["chg_pct"] is not None and abs(out[s]["chg_pct"]) > 8:
            out[s]["roll_warning"] = "單日變動過大，可能是期貨換月，請以新聞報導的漲跌幅為準"
        if s in ("SPY", "SOXX", "QQQ", "^GSPC", "^SOX"):
            frame = close.to_frame("close")
            frame["obs"] = frame.index
            w = frame.resample("W-WED").last().dropna().tail(9)
            weekly[s] = [{"date": r.obs.strftime("%Y-%m-%d"), "close": _num(r.close, 2)} for r in w.itertuples()]
    return out, weekly


def fred(series_id, days=200):
    # 注意：FRED 會擋瀏覽器 User-Agent，這裡刻意不帶 UA
    start = (dt.date.today() - dt.timedelta(days=days)).isoformat()
    url = f"https://fred.stlouisfed.org/graph/fredgraph.csv?id={series_id}&cosd={start}"
    last_err = None
    for _ in range(3):
        try:
            r = requests.get(url, timeout=45)
            r.raise_for_status()
            break
        except requests.RequestException as e:
            last_err = e
    else:
        raise last_err
    df = pd.read_csv(io.StringIO(r.text))
    df.columns = ["date", "value"]
    df["value"] = pd.to_numeric(df["value"], errors="coerce")
    df = df.dropna()
    df["date"] = pd.to_datetime(df["date"])
    return df.set_index("date")["value"]


def fetch_yields():
    out = {}
    for sid, name in (("DGS2", "美債 2 年"), ("DGS10", "美債 10 年"), ("DGS30", "美債 30 年")):
        try:
            s = fred(sid, 60)
            last, prev = float(s.iloc[-1]), float(s.iloc[-2])
            out[sid] = {
                "name": name,
                "last": _num(last, 3),
                "date": s.index[-1].strftime("%Y-%m-%d"),
                "chg_bp": _num((last - prev) * 100, 1),
            }
        except Exception as e:  # noqa: BLE001
            out[sid] = {"name": name, "error": str(e)}
    if "last" in out.get("DGS10", {}) and "last" in out.get("DGS2", {}):
        out["spread_10_2_bp"] = _num((out["DGS10"]["last"] - out["DGS2"]["last"]) * 100, 1)
    return out


def fetch_net_liquidity():
    """淨流動性 = Fed 資產(WALCL) − TGA(WTREGEN) − 隔夜逆回購(RRPONTSYD)，單位：兆美元。"""
    walcl = fred("WALCL")  # 百萬美元，每週三
    tga = fred("WTREGEN")  # 百萬美元，每週三
    rrp = fred("RRPONTSYD")  # 十億美元，每日
    rows = []
    for d, w in walcl.tail(9).items():
        t = tga[tga.index <= d]
        r = rrp[rrp.index <= d]
        if not len(t) or not len(r):
            continue
        nl = (w - float(t.iloc[-1]) - float(r.iloc[-1]) * 1000) / 1e6
        rows.append({"date": d.strftime("%Y-%m-%d"), "value": _num(nl, 3)})
    res = {"series": rows, "unit": "兆美元", "formula": "Fed 資產 − TGA − 隔夜逆回購"}
    if len(rows) >= 2:
        res["last"] = rows[-1]["value"]
        res["date"] = rows[-1]["date"]
        res["wk_chg_bn"] = _num((rows[-1]["value"] - rows[-2]["value"]) * 1000, 1)
    return res


def fetch_fear_greed():
    url = "https://production.dataviz.cnn.io/index/fearandgreed/graphdata"
    headers = {**UA, "Referer": "https://edition.cnn.com/", "Origin": "https://edition.cnn.com"}
    r = requests.get(url, headers=headers, timeout=30)
    r.raise_for_status()
    fg = r.json()["fear_and_greed"]
    rating = str(fg.get("rating", "")).lower()
    return {
        "score": _num(fg.get("score"), 0),
        "rating": FG_RATING.get(rating, rating),
        "prev_close": _num(fg.get("previous_close"), 0),
        "prev_week": _num(fg.get("previous_1_week"), 0),
        "timestamp": fg.get("timestamp"),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--extra", default="", help="額外要抓的代號，逗號分隔，例如 NVDA,ORCL")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "market.json"))
    args = ap.parse_args()

    symbols = list(TICKERS)
    for s in filter(None, (x.strip().upper() for x in args.extra.split(","))):
        if s not in symbols:
            symbols.append(s)

    result = {"generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M"), "errors": {}}
    steps = {
        "prices": lambda: fetch_prices(symbols),
        "yields": fetch_yields,
        "net_liquidity": fetch_net_liquidity,
        "fear_greed": fetch_fear_greed,
    }
    for key, fn in steps.items():
        try:
            val = fn()
            if key == "prices":
                result["prices"], result["weekly"] = val
            else:
                result[key] = val
        except Exception as e:  # noqa: BLE001
            result["errors"][key] = f"{type(e).__name__}: {e}"
            print(f"[warn] {key} 失敗：{e}", file=sys.stderr)

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    n = len(result.get("prices", {}))
    print(f"market.json 已寫入：{n} 檔報價，錯誤 {len(result['errors'])} 項 {list(result['errors'])}")


if __name__ == "__main__":
    main()
