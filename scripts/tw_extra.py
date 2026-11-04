"""台股日報的延伸資料（由 fetch_tw.py --full 呼叫）。

- 大盤與類股：類股指數漲跌、漲跌家數、成交值／漲幅／跌幅排行（證交所 MI_INDEX）、櫃買指數
- 期權籌碼：選擇權 Put/Call 比、最大未平倉履約價、十大交易人台指期部位（期交所）、借券賣出（證交所 TWT93U）
- 重大訊息與行事曆：重大訊息、月營收（證交所 OpenAPI）、處置股、注意股、除權息預告
- 亞股與匯率：日、韓、陸、港股與新台幣（Yahoo Finance）

範圍以上市為主；櫃買指數與亞股用 Yahoo Finance。
"""
import datetime as dt
import io
import re

import pandas as pd

LOT = 1000


def _num(x):
    try:
        return float(str(x).replace(",", "").replace("+", "").strip())
    except (TypeError, ValueError):
        return None


def _roc_to_iso(s):
    """'115年11月06日' 或 '1151103' → '2026-11-06'。"""
    s = str(s).strip()
    m = re.match(r"(\d{2,3})年(\d{1,2})月(\d{1,2})日", s) or re.match(r"(\d{3})(\d{2})(\d{2})$", s) or re.match(r"(\d{2,3})/(\d{1,2})/(\d{1,2})", s)
    if not m:
        return None
    y, mo, d = (int(x) for x in m.groups())
    return dt.date(y + 1911, mo, d).isoformat()


# ---------- 大盤與類股 ----------
def market_overview(mi):
    """mi：證交所 MI_INDEX（type=ALLBUT0999）的 JSON。"""
    out = {"sectors": [], "breadth": {}, "top_turnover": [], "top_gainers": [], "top_losers": []}
    for t in mi.get("tables", []):
        title, f, data = t.get("title") or "", t.get("fields") or [], t.get("data") or []
        if "價格指數(臺灣證券交易所)" in title:
            for r in data:
                name = r[0].strip()
                if name.endswith("類指數") and "未含" not in name:
                    # 漲跌百分比欄位本身已帶正負號（例如 '-0.81'），不要再乘一次符號
                    pct = str(r[4]).replace(",", "").strip()
                    out["sectors"].append({"name": name.replace("類指數", ""), "close": _num(r[1]),
                                           "chg_pct": float(pct) if re.fullmatch(r"[-+]?\d+(\.\d+)?", pct) else 0.0})
        elif title.startswith("漲跌證券數合計"):
            for r in data:
                out["breadth"][r[0].strip()] = r[2].strip()  # 「股票」欄，例如 '425(12)' 括號內是漲停／跌停家數
        elif "證券代號" in f and "收盤價" in f:
            ix = {k: f.index(k) for k in ("證券代號", "證券名稱", "成交金額", "收盤價", "漲跌(+/-)", "漲跌價差")}
            rows = []
            for r in data:
                code = r[ix["證券代號"]].strip()
                close = _num(r[ix["收盤價"]])
                if not (len(code) == 4 and code.isdigit()) or not close:
                    continue
                diff = _num(r[ix["漲跌價差"]]) or 0
                sign = -1 if "-" in str(r[ix["漲跌(+/-)"]]) else 1
                diff *= sign
                prev = close - diff
                rows.append({
                    "code": code,
                    "name": r[ix["證券名稱"]].strip(),
                    "close": close,
                    "chg": round(diff, 2),
                    "chg_pct": round(diff / prev * 100, 2) if prev else None,
                    "turnover_e8": round((_num(r[ix["成交金額"]]) or 0) / 1e8, 2),
                })
            out["top_turnover"] = sorted(rows, key=lambda x: -x["turnover_e8"])[:10]
            liquid = [x for x in rows if x["turnover_e8"] >= 1 and x["chg_pct"] is not None]  # 成交值 1 億以上，避免冷門股
            out["top_gainers"] = sorted(liquid, key=lambda x: -x["chg_pct"])[:10]
            out["top_losers"] = sorted(liquid, key=lambda x: x["chg_pct"])[:10]
    out["sectors"].sort(key=lambda x: -x["chg_pct"])

    def parse(v):
        m = re.match(r"([\d,]+)\(([\d,]+)\)", v or "")
        return (int(m.group(1).replace(",", "")), int(m.group(2).replace(",", ""))) if m else (None, None)

    b = out["breadth"]
    up, limit_up = parse(next((v for k, v in b.items() if k.startswith("上漲")), ""))
    down, limit_down = parse(next((v for k, v in b.items() if k.startswith("下跌")), ""))
    flat = _num(next((v for k, v in b.items() if k.startswith("持平")), None))
    out["breadth"] = {"up": up, "limit_up": limit_up, "down": down, "limit_down": limit_down, "flat": int(flat) if flat is not None else None}
    return out


def tpex_index(session):
    """櫃買指數（櫃買中心 OpenAPI）。"""
    rows = session.get("https://www.tpex.org.tw/openapi/v1/tpex_index", timeout=40).json()
    rows = sorted(rows, key=lambda r: r["Date"])
    last = rows[-1]
    close, chg = _num(last["Close"]), _num(last["Change"])
    return {"date": dt.datetime.strptime(last["Date"], "%Y%m%d").date().isoformat(), "close": close, "chg": chg,
            "chg_pct": round(chg / (close - chg) * 100, 2) if close and chg is not None else None}


def asia_fx():
    import yfinance as yf

    tickers = {
        "^N225": "日經 225",
        "^KS11": "韓國 KOSPI",
        "000001.SS": "上證指數",
        "^HSI": "恆生指數",
        "TWD=X": "美元/台幣",
        "JPY=X": "美元/日圓",
        "KRW=X": "美元/韓元",
        "CNY=X": "美元/人民幣",
    }
    df = yf.download(list(tickers), period="1mo", interval="1d", auto_adjust=False, progress=False, group_by="ticker", threads=True)
    out = {}
    for s, name in tickers.items():
        try:
            c = df[s]["Close"].dropna()
        except KeyError:
            continue
        if len(c) < 2:
            continue
        last, prev = float(c.iloc[-1]), float(c.iloc[-2])
        out[s] = {"name": name, "last": round(last, 4), "date": c.index[-1].strftime("%Y-%m-%d"), "chg_pct": round((last / prev - 1) * 100, 2)}
    return out


# ---------- 期權籌碼 ----------
def _taifex_csv(session, url, data):
    r = session.post(url, data=data, timeout=40)
    text = r.content.decode("big5", errors="replace")
    if not text.startswith(("日期", "交易日期")):
        raise RuntimeError(f"期交所沒有回傳資料：{text[:60]!r}")
    df = pd.read_csv(io.StringIO(text), dtype=str, index_col=False).fillna("")
    df.columns = [c.strip() for c in df.columns]
    return df.apply(lambda s: s.str.strip())


def options_chips(session, d):
    q = {"queryStartDate": (d - dt.timedelta(days=10)).strftime("%Y/%m/%d"), "queryEndDate": d.strftime("%Y/%m/%d")}
    out = {}

    pc = _taifex_csv(session, "https://www.taifex.com.tw/cht/3/pcRatioDown", q).sort_values("日期")
    last = pc.iloc[-1]
    prev = pc.iloc[-2] if len(pc) > 1 else None
    out["pc_ratio"] = {
        "date": last["日期"],
        "oi_ratio": _num(last["買賣權未平倉量比率%"]),
        "oi_ratio_prev": _num(prev["買賣權未平倉量比率%"]) if prev is not None else None,
        "vol_ratio": _num(last["買賣權成交量比率%"]),
    }

    day = d.strftime("%Y/%m/%d")
    opt = _taifex_csv(session, "https://www.taifex.com.tw/cht/3/optDataDown",
                      {"down_type": "1", "commodity_id": "TXO", "queryStartDate": day, "queryEndDate": day})
    opt = opt[opt["交易時段"] == "一般"]
    if len(opt):
        monthly = sorted(m for m in set(opt["到期月份(週別)"]) if re.fullmatch(r"\d{6}", m))
        weekly = sorted(m for m in set(opt["到期月份(週別)"]) if not re.fullmatch(r"\d{6}", m))
        res = {}
        for label, month in (("monthly", monthly[0] if monthly else None), ("weekly", weekly[0] if weekly else None)):
            if not month:
                continue
            sub = opt[opt["到期月份(週別)"] == month].copy()
            sub["oi"] = sub["未沖銷契約數"].map(lambda v: _num(v) or 0)
            sub["strike"] = sub["履約價"].map(_num)
            calls, puts = sub[sub["買賣權"] == "買權"], sub[sub["買賣權"] == "賣權"]
            if len(calls) and len(puts):
                c, p = calls.loc[calls["oi"].idxmax()], puts.loc[puts["oi"].idxmax()]
                res[label] = {"contract": month, "call_max_strike": int(c["strike"]), "call_max_oi": int(c["oi"]),
                              "put_max_strike": int(p["strike"]), "put_max_oi": int(p["oi"])}
        out["max_oi"] = res

    lt = _taifex_csv(session, "https://www.taifex.com.tw/cht/3/largeTraderFutDown", {"queryStartDate": day, "queryEndDate": day})
    tx = lt[(lt["商品(契約)"] == "TX") & (lt["到期月份(週別)"] == "999999")]
    for kind, key in (("0", "all"), ("1", "institutional")):
        row = tx[tx["交易人類別"] == kind]
        if len(row):
            r = row.iloc[0]
            out.setdefault("large_traders", {})[key] = {
                "top5_net": int(_num(r["前五大交易人買方"]) - _num(r["前五大交易人賣方"])),
                "top10_net": int(_num(r["前十大交易人買方"]) - _num(r["前十大交易人賣方"])),
                "market_oi": int(_num(r["全市場未沖銷部位數"])),
            }
    return out


def sbl(twse, d, names):
    """借券賣出（TWT93U 後半段欄位）。"""
    j = twse("marginTrading/TWT93U", date=d.strftime("%Y%m%d"))
    rows = []
    for r in (j or {}).get("data", []):
        code = r[0].strip()
        if not (len(code) == 4 and code.isdigit()):
            continue
        sold, ret, bal, prev = _num(r[9]) or 0, _num(r[10]) or 0, _num(r[12]) or 0, _num(r[8]) or 0
        rows.append({"code": code, "name": names.get(code) or r[1].strip(), "sold_lots": round(sold / LOT),
                     "returned_lots": round(ret / LOT), "balance_lots": round(bal / LOT), "chg_lots": round((bal - prev) / LOT)})
    return {
        "top_sold": sorted(rows, key=lambda x: -x["sold_lots"])[:10],
        "top_increase": sorted(rows, key=lambda x: -x["chg_lots"])[:5],
    }


# ---------- 重大訊息與行事曆 ----------
IMPORTANT = re.compile(r"營收|財測|財務預測|併購|合併|收購|取得|處分|增資|減資|股利|配息|法說|澄清|重大|簽約|訂單|擴產|投資|停工|裁罰|自結|獲利|虧損")


def events(session, twse, d, big_codes):
    out = {}
    roc = f"{d.year - 1911}{d.month:02d}{d.day:02d}"

    news = session.get("https://openapi.twse.com.tw/v1/opendata/t187ap04_L", timeout=40).json()
    # OpenAPI 可能還停在前一天，取「今天或之前最新的發言日期」
    dates = sorted({str(n.get("發言日期")) for n in news if str(n.get("發言日期", "")).isdigit() and str(n.get("發言日期")) <= roc})
    news_day = dates[-1] if dates else roc
    out["major_news_date"] = _roc_to_iso(news_day)
    items = []
    for n in news:
        if str(n.get("發言日期")) != news_day:
            continue
        title = (n.get("主旨 ") or n.get("主旨") or "").strip()
        code = str(n.get("公司代號", "")).strip()
        items.append({"code": code, "name": n.get("公司名稱", "").strip(), "title": title, "time": str(n.get("發言時間", "")).zfill(6)[:4],
                      "important": bool(IMPORTANT.search(title)), "big": code in big_codes})
    items.sort(key=lambda x: (not x["big"], not x["important"]))
    out["major_news"] = items[:40]
    out["major_news_total"] = len(items)

    rev = session.get("https://openapi.twse.com.tw/v1/opendata/t187ap05_L", timeout=40).json()
    rows = []
    for r in rev:
        cur = _num(r.get("營業收入-當月營收"))
        yoy = _num(r.get("營業收入-去年同月增減(%)"))
        if cur is None or yoy is None:
            continue
        rows.append({"code": r["公司代號"], "name": r["公司名稱"], "month": r.get("資料年月"), "revenue_e8": round(cur / 1e5, 2),
                     "yoy": round(yoy, 1), "mom": round(_num(r.get("營業收入-上月比較增減(%)")) or 0, 1),
                     "ytd_yoy": round(_num(r.get("累計營業收入-前期比較增減(%)")) or 0, 1)})
    big = [x for x in rows if x["revenue_e8"] >= 50]  # 月營收 50 億以上的大型公司
    out["revenue"] = {
        "month": rows[0]["month"] if rows else None,
        "reported": len(rows),
        "big_yoy_top": sorted(big, key=lambda x: -x["yoy"])[:8],
        "big_yoy_bottom": sorted(big, key=lambda x: x["yoy"])[:5],
        "leaders": [x for x in rows if x["code"] in ("2330", "2317", "2454", "2308", "2382", "3711")],
    }

    j = twse("announcement/punish") or {}
    f = j.get("fields") or []
    punish, seen = [], set()
    for r in j.get("data", []):
        rec = dict(zip(f, r))
        period = rec.get("處置起迄時間", "")
        dates = re.findall(r"\d{3}/\d{2}/\d{2}", period)
        code = rec.get("證券代號", "").strip()
        if len(dates) == 2 and _roc_to_iso(dates[0]) <= d.isoformat() <= _roc_to_iso(dates[1]) and code not in seen:
            seen.add(code)  # 同一檔有重疊的處置期間時只列一次（公告依新到舊排列，保留最新那筆）
            punish.append({"code": rec.get("證券代號", "").strip(), "name": rec.get("證券名稱", "").strip(), "period": period.strip(),
                           "measure": rec.get("處置措施", "").strip()})
    out["punish"] = punish

    j = twse("announcement/notice", startDate=d.strftime("%Y%m%d"), endDate=d.strftime("%Y%m%d")) or {}
    f = j.get("fields") or []
    notice = []
    for r in j.get("data", []):
        rec = dict(zip(f, r))
        code = rec.get("證券代號", "").strip()
        if len(code) == 4 and code.isdigit():  # 排除權證等衍生商品
            notice.append({"code": code, "name": rec.get("證券名稱", "").strip(), "count": rec.get("累計次數", "").strip()})
    out["notice"] = notice

    j = twse("exRight/TWT48U") or {}
    f = j.get("fields") or []
    ex, horizon = [], (d + dt.timedelta(days=10)).isoformat()
    for r in j.get("data", []):
        rec = dict(zip(f, r))
        day = _roc_to_iso(rec.get("除權除息日期", ""))
        code = rec.get("股票代號", "").strip()
        if day and d.isoformat() < day <= horizon and len(code) == 4:
            cash = _num(rec.get("現金股利"))
            ex.append({"date": day, "code": code, "name": rec.get("名稱", "").strip(), "type": rec.get("除權息", "").strip(), "cash": cash})
    out["ex_dividend"] = sorted(ex, key=lambda x: x["date"])[:15]
    return out
