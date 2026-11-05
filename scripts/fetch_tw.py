"""抓取前一交易日台股籌碼，輸出 data/tw.json。

內容：
- 大盤：加權指數、成交金額（證交所 FMTQIK）
- 三大法人買賣金額（證交所 BFI82U）、個股買賣超（T86）
- 外資／投信／自營商台指期未平倉（期交所）
- 信用交易：融資融券餘額與個股增減（證交所 MI_MARGN）
- 融資維持率估算：「若再跌一根（跌停 10%）維持率會跌破 130%」的個股
- 主動式 ETF 持股變化：00981A（統一投信）、00992A（群益投信）

注意：
- 範圍為上市股票（不含上櫃）。
- 融資維持率是估算值：以近 N 個交易日融資餘額增減 × 收盤價，用均價法推算融資平均成本，
  融資成數假設 60%。實際維持率依各券商與個人成本而不同，僅供參考。

用法：python scripts/fetch_tw.py [--days 30]
"""
import argparse
import datetime as dt
import io
import json
import os
import sys
import time
import warnings
from zoneinfo import ZoneInfo

warnings.filterwarnings("ignore")

import pandas as pd  # noqa: E402
import requests  # noqa: E402

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TW = ZoneInfo("Asia/Taipei")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
WEEKDAY_ZH = "一二三四五六日"
LOT = 1000  # 1 張 = 1000 股
MARGIN_RATIO = 0.6  # 上市融資成數
LIMIT_DOWN = 0.10
ETF_BIG_LOTS = 100  # 增減 100 張以上特別標示

S = requests.Session()
S.headers["User-Agent"] = UA


def num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def parse_date(v):
    """支援 '2026-10-29T00:00:00' 與 ASP.NET 的 '/Date(1793232000000)/' 兩種格式。"""
    s = str(v)
    if s.startswith("/Date("):
        ms = int(s[6:].split(")")[0].split("+")[0].split("-")[0])
        return dt.datetime.fromtimestamp(ms / 1000, TW).date().isoformat()
    return s[:10]


def roc(d):
    return f"{d.year - 1911}/{d.month:02d}/{d.day:02d}"


def twse(path, **params):
    for attempt in range(3):
        try:
            r = S.get(f"https://www.twse.com.tw/rwd/zh/{path}", params={**params, "response": "json"}, timeout=40)
            time.sleep(1.6)  # 證交所有頻率限制
            j = r.json()
            return j if j.get("stat") == "OK" else None
        except (requests.RequestException, ValueError):
            time.sleep(5 * (attempt + 1))
    return None


# ---------- 信用交易（同時用來找交易日） ----------
def margin_history(n_days, budget=0):
    """由今天往回找 n_days 個有融資資料的交易日，回傳 [(date, summary_table, {code: row})]，舊到新。
    budget > 0 時：超過 budget 秒且已取得至少 5 天就停止（雲端連證交所很慢，避免晨報卡住）。"""
    out = []
    d = dt.datetime.now(TW).date()
    tries = 0
    t0 = time.time()
    while len(out) < n_days and tries < n_days * 2 + 15:
        if budget and len(out) >= 5 and time.time() - t0 > budget:
            print(f"[warn] 融資歷史已用 {time.time() - t0:.0f} 秒，只取 {len(out)} 天就停止", file=sys.stderr)
            break
        tries += 1
        if d.weekday() < 5:
            j = twse("marginTrading/MI_MARGN", date=d.strftime("%Y%m%d"), selectType="ALL")
            if j and len(j.get("tables", [])) > 1 and j["tables"][1].get("data"):
                rows = {}
                for r in j["tables"][1]["data"]:
                    code = r[0].strip()
                    rows[code] = {
                        "name": r[1].strip(),
                        "fin_prev": num(r[5]),
                        "fin": num(r[6]),
                        "short_prev": num(r[11]),
                        "short": num(r[12]),
                    }
                out.append((d, j["tables"][0]["data"], rows))
        d -= dt.timedelta(days=1)
    return list(reversed(out))


def margin_summary(table):
    res = {}
    for r in table:
        label = r[0]
        prev, today = num(r[4]), num(r[5])
        key = "fin_lots" if label.startswith("融資(") else "short_lots" if label.startswith("融券(") else "fin_amt_k" if label.startswith("融資金額") else None
        if key:
            res[key] = {"prev": prev, "today": today, "chg": (today - prev) if prev is not None and today is not None else None}
    return res


# ---------- 收盤價 ----------
MI_CACHE = {}


def closes_today(d):
    j = twse("afterTrading/MI_INDEX", date=d.strftime("%Y%m%d"), type="ALLBUT0999")
    MI_CACHE[d] = j or {}
    out = {}
    if not j:
        return out
    for t in j.get("tables", []):
        f = t.get("fields") or []
        if "證券代號" in f and "收盤價" in f:
            ic, ip = f.index("證券代號"), f.index("收盤價")
            for r in t.get("data", []):
                p = num(r[ip])
                if p:
                    out[r[ic].strip()] = p
    return out


def close_history(codes, start):
    import yfinance as yf

    tickers = [f"{c}.TW" for c in codes]
    df = yf.download(tickers, start=start.isoformat(), interval="1d", auto_adjust=False, progress=False, group_by="ticker", threads=True)
    out = {}
    for c, t in zip(codes, tickers):
        try:
            s = df[t]["Close"].dropna() if len(tickers) > 1 else df["Close"].dropna()
        except KeyError:
            continue
        out[c] = {i.strftime("%Y-%m-%d"): float(v) for i, v in s.items()}
    return out


# ---------- 融資維持率估算 ----------
def maintenance_risk(hist, last_close, min_lots=1000, top=20):
    last_rows = hist[-1][2]
    cands = [
        c for c, r in last_rows.items()
        if len(c) == 4 and c.isdigit() and (r["fin"] or 0) >= min_lots and c in last_close
    ]
    ph = close_history(cands, hist[0][0] - dt.timedelta(days=7))
    risky, broken = [], []
    for c in cands:
        prices = ph.get(c) or {}
        bal = cost = None
        for d, _, rows in hist:
            r = rows.get(c)
            p = prices.get(d.isoformat())
            if not r or p is None:
                continue
            if bal is None:
                bal, cost = r["fin"] or 0, p
                continue
            delta = (r["fin"] or 0) - (r["fin_prev"] or 0)
            if delta > 0:
                cost = p if bal <= 0 else (cost * bal + p * delta) / (bal + delta)
                bal += delta
            else:
                bal = max(bal + delta, 0)
        if not cost:
            continue
        close = last_close[c]
        ratio = close / (cost * MARGIN_RATIO) * 100
        after = ratio * (1 - LIMIT_DOWN)
        r = last_rows[c]
        item = {
            "code": c,
            "name": r["name"],
            "close": round(close, 2),
            "est_cost": round(cost, 2),
            "ratio": round(ratio, 1),
            "ratio_after": round(after, 1),
            "fin_lots": int(r["fin"]),
            "fin_chg": int((r["fin"] or 0) - (r["fin_prev"] or 0)),
        }
        if ratio < 130:
            broken.append(item)
        elif after < 130:
            risky.append(item)
    risky.sort(key=lambda x: x["ratio"])
    broken.sort(key=lambda x: x["ratio"])
    return {"next_limit_down": risky[:top], "already_below": broken[:10], "candidates": len(cands), "lookback_days": len(hist)}


# ---------- 法人 ----------
def institutional(d, last_close):
    out = {}
    j = twse("fund/BFI82U", type="day", dayDate=d.strftime("%Y%m%d"))
    if j:
        out["summary"] = [{"name": r[0], "buy": num(r[1]), "sell": num(r[2]), "net": num(r[3])} for r in j["data"]]
    j = twse("fund/T86", date=d.strftime("%Y%m%d"), selectType="ALLBUT0999")
    if j:
        f = j["fields"]
        idx = {
            "foreign": f.index("外陸資買賣超股數(不含外資自營商)"),
            "trust": f.index("投信買賣超股數"),
            "dealer": f.index("自營商買賣超股數"),
        }
        rows = []
        for r in j["data"]:
            code = r[0].strip()
            if not (len(code) == 4 and code.isdigit()):
                continue
            p = last_close.get(code)
            item = {"code": code, "name": r[1].strip()}
            for k, i in idx.items():
                sh = num(r[i]) or 0
                item[f"{k}_lots"] = round(sh / LOT)
                item[f"{k}_amt_e8"] = round(sh * p / 1e8, 2) if p else None
            rows.append(item)
        def top(key, rev, n=10):
            arr = [x for x in rows if x[key] is not None]
            arr.sort(key=lambda x: x[key], reverse=rev)
            return [x for x in arr[:n] if (x[key] > 0 if rev else x[key] < 0)]
        out["foreign_buy"] = top("foreign_amt_e8", True)
        out["foreign_sell"] = top("foreign_amt_e8", False)
        out["trust_buy"] = top("trust_amt_e8", True)
        out["trust_sell"] = top("trust_amt_e8", False)
    return out


def taifex_oi(d):
    start = d - dt.timedelta(days=10)
    r = S.post(
        "https://www.taifex.com.tw/cht/3/futContractsDateDown",
        data={"queryStartDate": start.strftime("%Y/%m/%d"), "queryEndDate": d.strftime("%Y/%m/%d"), "commodityId": "TXF"},
        timeout=40,
    )
    df = pd.read_csv(io.StringIO(r.content.decode("big5", errors="replace")), index_col=False)
    df.columns = [c.strip() for c in df.columns]
    out = {}
    for who, key in (("外資及陸資", "foreign"), ("投信", "trust"), ("自營商", "dealer")):
        s = df[df["身份別"].str.strip() == who].sort_values("日期")
        if len(s):
            last = s.iloc[-1]
            prev = s.iloc[-2] if len(s) > 1 else None
            oi = int(last["多空未平倉口數淨額"])
            out[key] = {
                "date": str(last["日期"]),
                "net_oi": oi,
                "chg": oi - int(prev["多空未平倉口數淨額"]) if prev is not None else None,
                "net_trade": int(last["多空交易口數淨額"]),
            }
    return out


def taifex_night():
    """台指期近月夜盤（期交所標記為「盤後」，歸屬下一個交易日）。"""
    today = dt.datetime.now(TW).date()
    df = None
    for back in range(5):  # 結束日期還沒有資料時，期交所會回錯誤頁，往前找
        end = today - dt.timedelta(days=back)
        r = S.post(
            "https://www.taifex.com.tw/cht/3/futDataDown",
            data={"down_type": "1", "commodity_id": "TX", "queryStartDate": (end - dt.timedelta(days=10)).strftime("%Y/%m/%d"), "queryEndDate": end.strftime("%Y/%m/%d")},
            timeout=40,
        )
        text = r.content.decode("big5", errors="replace")
        if text.startswith("交易日期"):
            # 每行結尾多一個逗號，index_col=False 避免欄位錯位
            df = pd.read_csv(io.StringIO(text), dtype=str, index_col=False).fillna("")
            break
        time.sleep(1)
    if df is None:
        raise RuntimeError("期交所沒有回傳期貨行情")
    df.columns = [c.strip() for c in df.columns]
    df = df.apply(lambda s: s.str.strip())
    df = df[(df["契約"] == "TX") & df["到期月份(週別)"].str.fullmatch(r"\d{6}")]
    out = {}
    day_dates = sorted(set(df[df["交易時段"] == "一般"]["交易日期"]))
    for session, key in (("盤後", "night"), ("一般", "day")):
        s = df[df["交易時段"] == session]
        if not len(s):
            continue
        last_date = s["交易日期"].max()
        row = s[s["交易日期"] == last_date].sort_values("到期月份(週別)").iloc[0]
        if key == "night":
            # 期交所把夜盤歸屬到「下一個交易日」：交易日 D 的盤後 = D 的前一個交易日晚上
            prev = [x for x in day_dates if x < last_date]
            out["night_evening"] = prev[-1] if prev else None
        out[key] = {
            "trade_date": last_date,
            "month": row["到期月份(週別)"],
            "close": num(row["收盤價"]),
            "chg": num(row["漲跌價"]),
            "chg_pct": num(row["漲跌%"].replace("%", "")),
            "volume": num(row["成交量"]),
        }
    return out


# ---------- 主動式 ETF ----------
def etf_00981a():
    """統一投信 ezmoney：GetPCF 可依「公告日（民國年）」查詢，回傳前一交易日持股。"""
    sess = requests.Session()
    sess.headers["User-Agent"] = UA

    def call(method, url, **kw):
        # 投信網站偶爾會中斷連線（Connection reset），重試幾次
        for attempt in range(4):
            try:
                return getattr(sess, method)(url, timeout=40, **kw)
            except requests.RequestException:
                time.sleep(3 * (attempt + 1))
        return None

    call("get", "https://www.ezmoney.com.tw/ETF/Transaction/PCF?fundCode=49YTW")
    snaps = {}
    d = dt.datetime.now(TW).date() + dt.timedelta(days=1)
    for _ in range(12):
        if len(snaps) >= 2:
            break
        r = call("post", "https://www.ezmoney.com.tw/ETF/Transaction/GetPCF", json={"fundCode": "49YTW", "date": roc(d), "specificDate": True})
        try:
            j = r.json() if r is not None else {}
        except ValueError:
            j = {}
        for a in j.get("asset") or []:
            if a.get("AssetCode") == "ST" and a.get("Details"):
                tran = parse_date(a["Details"][0]["TranDate"])
                snaps.setdefault(tran, {x["DetailCode"].strip(): {"name": (x.get("DetailName") or "").strip(), "shares": x["Share"], "weight": x.get("NavRate")} for x in a["Details"]})
        d -= dt.timedelta(days=1)
        time.sleep(0.5)
    return snaps


def etf_00992a():
    """群益投信：/CFWeb/api/etf/buyback 可帶 date（公告日 YYYY-MM-DD），stocks 為持股。"""
    snaps = {}
    d = dt.datetime.now(TW).date() + dt.timedelta(days=1)
    for _ in range(12):
        if len(snaps) >= 2:
            break
        try:
            r = S.post("https://www.capitalfund.com.tw/CFWeb/api/etf/buyback", json={"fundId": "500", "date": d.isoformat()}, timeout=40)
            data = r.json().get("data") or {}
        except (requests.RequestException, ValueError):
            data = {}
        pcf, stocks = data.get("pcf") or {}, data.get("stocks") or []
        if stocks and pcf.get("date2"):
            snaps.setdefault(pcf["date2"][:10], {s["stocNo"].strip(): {"name": (s.get("stocName") or "").strip(), "shares": s["share"], "weight": s.get("weight")} for s in stocks})
        d -= dt.timedelta(days=1)
        time.sleep(0.5)
    return snaps


def etf_diff(code, name, snaps, names):
    if len(snaps) < 2:
        return {"code": code, "name": name, "error": f"只取得 {len(snaps)} 天持股，無法比對"}
    dates = sorted(snaps)[-2:]
    prev, cur = snaps[dates[0]], snaps[dates[1]]
    changes = []
    for c in set(prev) | set(cur):
        a = (prev.get(c) or {}).get("shares") or 0
        b = (cur.get(c) or {}).get("shares") or 0
        if a == b:
            continue
        info = cur.get(c) or prev.get(c)
        lots = round((b - a) / LOT)
        changes.append({
            "code": c,
            "name": names.get(c) or info["name"],
            "lots_before": round(a / LOT),
            "lots_after": round(b / LOT),
            "chg_lots": lots,
            "status": "新增" if a == 0 else "出清" if b == 0 else ("加碼" if lots > 0 else "減碼"),
            "weight": (cur.get(c) or {}).get("weight"),
            "big": abs(lots) >= ETF_BIG_LOTS,
        })
    changes.sort(key=lambda x: -abs(x["chg_lots"]))
    return {
        "code": code,
        "name": name,
        "date_prev": dates[0],
        "date": dates[1],
        "holdings": len(cur),
        "changes": changes,
        "big": [x for x in changes if x["big"]],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=30, help="估算融資成本的回溯交易日數")
    ap.add_argument("--budget", type=int, default=0, help="融資歷史最多花幾秒（取得 5 天以上後才會停止）；0 = 不限")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "tw.json"))
    ap.add_argument("--night-only", action="store_true", help="只重抓台指期夜盤，更新既有的 tw.json")
    ap.add_argument("--full", action="store_true", help="台股日報用：另外抓大盤類股、期權、借券、重大訊息、行事曆、亞股匯率")
    ap.add_argument("--require-today", action="store_true", help="台股日報用：等證交所公布「今天」的融資資料；今天休市就結束（exit code 3）")
    ap.add_argument("--wait-minutes", type=int, default=40)
    args = ap.parse_args()

    if args.require_today:
        now = dt.datetime.now(TW)
        # 15:00 前（例如半夜手動重跑）把「今天的報告」視為前一天
        today = now.date() if now.hour >= 15 else now.date() - dt.timedelta(days=1)
        if not twse("fund/BFI82U", type="day", dayDate=today.strftime("%Y%m%d")):
            os.makedirs(os.path.dirname(args.out), exist_ok=True)
            with open(args.out, "w", encoding="utf-8") as f:
                json.dump({"holiday": True, "date": today.isoformat()}, f, ensure_ascii=False)
            print(f"{today} 沒有三大法人資料，判斷為休市，不產生台股日報")
            sys.exit(3)
        deadline = time.time() + args.wait_minutes * 60
        while True:
            j = twse("marginTrading/MI_MARGN", date=today.strftime("%Y%m%d"), selectType="MS")
            if j:
                print(f"{today} 融資融券資料已公布")
                break
            if time.time() > deadline:
                sys.exit(f"等了 {args.wait_minutes} 分鐘，證交所仍未公布 {today} 的融資融券資料")
            print("融資融券資料尚未公布，90 秒後再試…", flush=True)
            time.sleep(90)

    if args.night_only:
        with open(args.out, encoding="utf-8") as f:
            result = json.load(f)
        night = taifex_night()
        night["is_last_night"] = night.get("night_evening") == result["date"].replace("-", "/")
        result["tx_night"] = night
        with open(args.out, "w", encoding="utf-8") as f:
            json.dump(result, f, ensure_ascii=False, indent=2)
        print(f"夜盤已更新：{night.get('night_evening')} 晚 {night.get('night', {}).get('close')}｜是否為昨晚：{night['is_last_night']}")
        return

    result = {"errors": {}, "generated_at_utc": dt.datetime.now(dt.timezone.utc).strftime("%Y-%m-%d %H:%M")}

    def step(key, fn):
        try:
            result[key] = fn()
        except Exception as e:  # noqa: BLE001
            result["errors"][key] = f"{type(e).__name__}: {e}"
            print(f"[warn] {key} 失敗：{e}", file=sys.stderr)

    hist = margin_history(args.days, args.budget)
    if not hist:
        sys.exit("找不到任何交易日的融資資料")
    d = hist[-1][0]
    result["date"] = d.isoformat()
    result["label"] = f"{d.year}/{d.month}/{d.day}（{WEEKDAY_ZH[d.weekday()]}）"
    names = {c: r["name"] for c, r in hist[-1][2].items()}

    def index_info():
        j = twse("afterTrading/FMTQIK", date=d.strftime("%Y%m%d"))
        row = next((r for r in reversed(j["data"]) if r[0] == roc(d)), j["data"][-1])
        close, chg = num(row[4]), num(row[5])
        return {"close": close, "chg": chg, "chg_pct": round(chg / (close - chg) * 100, 2), "turnover_e8": round(num(row[2]) / 1e8)}

    step("index", index_info)
    last_close = closes_today(d)
    step("margin", lambda: margin_summary(hist[-1][1]))

    def margin_movers():
        rows = [
            {"code": c, "name": r["name"], "fin_lots": int(r["fin"] or 0), "chg": int((r["fin"] or 0) - (r["fin_prev"] or 0)),
             "short_chg": int((r["short"] or 0) - (r["short_prev"] or 0))}
            for c, r in hist[-1][2].items() if len(c) == 4 and c.isdigit()
        ]
        inc = sorted(rows, key=lambda x: -x["chg"])[:10]
        dec = sorted(rows, key=lambda x: x["chg"])[:10]
        short_inc = sorted(rows, key=lambda x: -x["short_chg"])[:5]
        return {"fin_inc": [x for x in inc if x["chg"] > 0], "fin_dec": [x for x in dec if x["chg"] < 0], "short_inc": [x for x in short_inc if x["short_chg"] > 0]}

    step("margin_movers", margin_movers)
    step("institutional", lambda: institutional(d, last_close))
    step("futures", lambda: taifex_oi(d))
    step("tx_night", taifex_night)
    if result.get("tx_night"):
        # 夜盤是否為「最近交易日（籌碼資料日）晚上」的那一場
        result["tx_night"]["is_last_night"] = result["tx_night"].get("night_evening") == d.strftime("%Y/%m/%d")
    step("maintenance", lambda: maintenance_risk(hist, last_close))
    # 主動式 ETF 持股變化已移到 scripts/etf_holdings.py（台股日報另外產生 ETF 共識與明細圖）

    if args.full:
        import tw_extra

        step("overview", lambda: tw_extra.market_overview(MI_CACHE.get(d) or {}))
        step("asia_fx", tw_extra.asia_fx)
        step("tpex", lambda: tw_extra.tpex_index(S))
        step("options", lambda: tw_extra.options_chips(S, d))
        step("sbl", lambda: tw_extra.sbl(twse, d, names))
        big = {x["code"] for x in (result.get("overview") or {}).get("top_turnover", [])} | {"2330", "2317", "2454", "2308", "2382", "2412", "2881", "2882", "3711", "2303"}
        step("events", lambda: tw_extra.events(S, twse, d, big))

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    m = result.get("maintenance") or {}
    print(
        f"tw.json 已寫入：{result['label']}｜維持率風險 {len(m.get('next_limit_down', []))} 檔（候選 {m.get('candidates')}、回溯 {m.get('lookback_days')} 日）｜"
        f"00981A 大幅增減 {len((result.get('etf_00981A') or {}).get('big', []))} 檔｜00992A {len((result.get('etf_00992A') or {}).get('big', []))} 檔｜錯誤 {list(result['errors'])}"
    )


if __name__ == "__main__":
    main()
