"""主動式與大型被動式 ETF 每日持股變化，輸出 data/etf.json 與 out/ETF持股變化_YYYY-MM-DD.xlsx。

追蹤名單：
- 主動式：上個月成交量前 30 名 ETF 裡的主動式股票 ETF（每月自動更新，月內固定，方便逐日比對）
- 被動式：規模最大的 5 檔股票型 ETF（PASSIVE，可手動調整）

資料來源（全部是各投信官網公告的持股，可指定日期）：
統一 ezmoney、群益 capitalfund、中信 ctbcinvestments、復華 fhtrust、富邦 fsit、國泰 cathaysite、元大 yuantaetfs
尚未支援：凱基、安聯（名單裡出現時會標示「尚未支援」）

用法：python scripts/etf_holdings.py [--days 5]
"""
import argparse
import datetime as dt
import json
import os
import re
import sys
import time
from zoneinfo import ZoneInfo

import requests

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TW = ZoneInfo("Asia/Taipei")
UA = "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/126.0 Safari/537.36"
LOT = 1000
BIG_LOTS = 100

# 各投信的 ETF 代碼對照（issuer, 投信內部代號）
REGISTRY = {
    "00981A": ("uni", "49YTW"), "00403A": ("uni", "63YTW"),
    "00992A": ("cap", "500"), "00415A": ("cap", "504"), "00982A": ("cap", "399"), "00919": ("cap", "195"),
    "00406A": ("ctbc", "E0038"),
    "00991A": ("fh", "ETF23"),
    "00405A": ("fubon", "00405A"), "006208": ("fubon", "006208"),
    "00400A": ("cathay", "EA"), "00878": ("cathay", "CN"),
    "0050": ("yuanta", "0050"), "0056": ("yuanta", "0056"),
    "00985A": ("nomura", "00985A"),
}
ISSUER_NAME = {"uni": "統一", "cap": "群益", "ctbc": "中信", "fh": "復華", "fubon": "富邦", "cathay": "國泰", "yuanta": "元大", "nomura": "野村"}
# 名稱含這些字的主動式 ETF 主要持有海外股票，不列入台股持股比對
OVERSEAS = re.compile(r"全球|美國|美股|亞|日本|中國|印度|歐洲|海外|國際")
PASSIVE = ["0050", "006208", "00878", "0056", "00919"]  # 規模前 5 大被動股票型 ETF

S = requests.Session()
S.headers["User-Agent"] = UA


def num(x):
    try:
        return float(str(x).replace(",", "").strip())
    except (TypeError, ValueError):
        return None


def iso(d):
    return d.isoformat() if isinstance(d, dt.date) else d


# ---------- 交易日與成交量排行（證交所） ----------
def twse(path, **params):
    for attempt in range(3):
        try:
            r = S.get(f"https://www.twse.com.tw/rwd/zh/{path}", params={**params, "response": "json"}, timeout=40)
            time.sleep(1.6)
            j = r.json()
            return j if j.get("stat") == "OK" else None
        except (requests.RequestException, ValueError):
            time.sleep(5 * (attempt + 1))
    return None


def trading_days(n_back_months=2):
    """近幾個月的交易日（由證交所每日成交統計取得）。"""
    today = dt.datetime.now(TW).date()
    days = set()
    for k in range(n_back_months):
        y, m = today.year, today.month - k
        while m <= 0:
            y, m = y - 1, m + 12
        j = twse("afterTrading/FMTQIK", date=f"{y}{m:02d}01")
        for r in (j or {}).get("data", []):
            yy, mm, dd = (int(x) for x in r[0].split("/"))
            days.add(dt.date(yy + 1911, mm, dd))
    return sorted(d for d in days if d <= today)


def last_month_top_active(days, names_out):
    """上個月成交量前 30 名 ETF 中的主動式股票 ETF。"""
    today = dt.datetime.now(TW).date()
    first = today.replace(day=1)
    prev_month = [d for d in days if d < first and d >= (first - dt.timedelta(days=1)).replace(day=1)]
    vol = {}
    for d in prev_month:
        j = twse("afterTrading/MI_INDEX", date=d.strftime("%Y%m%d"), type="ALLBUT0999") or {}
        for t in j.get("tables", []):
            f = t.get("fields") or []
            if "證券代號" in f and "成交股數" in f:
                ic, iv, iname = f.index("證券代號"), f.index("成交股數"), f.index("證券名稱")
                for r in t["data"]:
                    c = r[ic].strip()
                    if c.startswith("00"):
                        vol[c] = vol.get(c, 0) + (num(r[iv]) or 0)
                        names_out[c] = r[iname].strip()
    top30 = [c for c, _ in sorted(vol.items(), key=lambda x: -x[1])[:30]]
    active = [c for c in top30 if c.endswith("A")]
    return active, top30, (prev_month[0].isoformat() if prev_month else None)


def stock_names(d):
    """上市（證交所）＋上櫃（櫃買中心 OpenAPI）股票簡稱。"""
    out = {}
    try:
        for x in S.get("https://www.tpex.org.tw/openapi/v1/tpex_mainboard_daily_close_quotes", timeout=40).json():
            out[str(x.get("SecuritiesCompanyCode", "")).strip()] = str(x.get("CompanyName", "")).strip()
    except (requests.RequestException, ValueError):
        print("[warn] 取不到上櫃股票名稱", file=sys.stderr)
    j = twse("afterTrading/MI_INDEX", date=d.strftime("%Y%m%d"), type="ALLBUT0999") or {}
    for t in j.get("tables", []):
        f = t.get("fields") or []
        if "證券代號" in f and "證券名稱" in f:
            for r in t["data"]:
                out[r[f.index("證券代號")].strip()] = r[f.index("證券名稱")].strip()
    return out


# ---------- 各投信持股抓取：回傳 (持股日期 ISO, {股票代號: 股數}) ----------
def _retry(fn, tries=3):
    for i in range(tries):
        try:
            return fn()
        except (requests.RequestException, ValueError, KeyError, IndexError, TypeError):
            if i == tries - 1:
                raise
            time.sleep(3 * (i + 1))


_uni_session = None


def fetch_uni(fid, d):
    """統一：以「公告日」查詢，回傳前一交易日持股，所以要往後找公告日。"""
    global _uni_session
    if _uni_session is None:
        _uni_session = requests.Session()
        _uni_session.headers["User-Agent"] = UA
        _retry(lambda: _uni_session.get("https://www.ezmoney.com.tw/ETF/Transaction/PCF?fundCode=49YTW", timeout=40))
    for k in range(1, 7):
        post = d + dt.timedelta(days=k)
        roc = f"{post.year - 1911}/{post.month:02d}/{post.day:02d}"
        j = _retry(lambda: _uni_session.post("https://www.ezmoney.com.tw/ETF/Transaction/GetPCF",
                                             json={"fundCode": fid, "date": roc, "specificDate": True}, timeout=40).json())
        for a in j.get("asset") or []:
            if a.get("AssetCode") == "ST" and a.get("Details"):
                tran = _parse_ms(a["Details"][0]["TranDate"])
                if tran == d.isoformat():
                    return tran, {x["DetailCode"].strip(): x["Share"] for x in a["Details"]}
                if tran and tran > d.isoformat():
                    return None, {}
        time.sleep(0.4)
    return None, {}


def _parse_ms(v):
    s = str(v)
    if s.startswith("/Date("):
        ms = int(re.match(r"/Date\((\d+)", s).group(1))
        return dt.datetime.fromtimestamp(ms / 1000, TW).date().isoformat()
    return s[:10]


def fetch_cap(fid, d):
    """群益：date 是公告日，pcf.date2 是持股日期。"""
    for k in range(1, 7):
        post = d + dt.timedelta(days=k)
        j = _retry(lambda: S.post("https://www.capitalfund.com.tw/CFWeb/api/etf/buyback", json={"fundId": fid, "date": post.isoformat()}, timeout=40).json())
        data = j.get("data") or {}
        hold = ((data.get("pcf") or {}).get("date2") or "")[:10]
        if hold == d.isoformat() and data.get("stocks"):
            return hold, {s["stocNo"].strip(): s["share"] for s in data["stocks"]}
        if hold and hold > d.isoformat():
            return None, {}
        time.sleep(0.4)
    return None, {}


_ctbc_token = None


def fetch_ctbc(fid, d):
    global _ctbc_token
    base = "https://www.ctbcinvestments.com/API/"
    h = {"Origin": "https://www.ctbcinvestments.com", "Referer": "https://www.ctbcinvestments.com/"}

    def post(path, tok, **data):
        return S.post(base + path, params={"token": tok}, json={"token": tok, **data}, headers=h, timeout=40).json()

    if _ctbc_token is None:
        _ctbc_token = _retry(lambda: post("home/AuthToken", "www.ctbcinvestments.com")["Data"]["token"])
    j = _retry(lambda: post("etf/ETFHoldingWeight", _ctbc_token, FID=fid, StartDate=d.strftime("%Y/%m/%d"))["Data"])
    hold = ((j.get("FundAssets") or [{}])[0].get("資料日期") or "").replace("/", "-")
    stocks = next((g["Data"] for g in j.get("FundAssetsDetail") or [] if g.get("Code") == "STOCK"), [])
    if hold != d.isoformat():
        return None, {}
    return hold, {x["code_"].strip(): num(x["qty_"]) for x in stocks if x.get("code_")}


def fetch_fh(fid, d):
    j = _retry(lambda: S.get("https://www.fhtrust.com.tw/api/assets", params={"fundID": fid, "qDate": d.strftime("%Y/%m/%d")}, timeout=40).json())
    r = (j.get("result") or [{}])[0]
    hold = (r.get("dDate") or "").replace("/", "-")
    if hold != d.isoformat():
        return None, {}
    return hold, {x["stockid"].strip(): num(x["qshare"]) for x in r.get("detail") or [] if x.get("ftype") == "股票"}


def fetch_fubon(fid, d):
    url = f"https://websys.fsit.com.tw/FubonETF/Trade/Assets.aspx?stkId={fid}&lan=TW"
    sess = requests.Session()
    sess.headers["User-Agent"] = UA
    page = _retry(lambda: sess.get(url, timeout=40).text)
    cur = re.search(r'sDate"[^>]*value="([^"]*)"', page)
    if not (cur and cur.group(1) == d.strftime("%Y/%m/%d")):
        fields = dict(re.findall(r'name="(__VIEWSTATE|__EVENTVALIDATION|__VIEWSTATEGENERATOR)"[^>]*value="([^"]*)"', page))
        p = "ctl00$ctl00$mainContent$subMainContent$"
        data = {**fields, p + "sDate": d.strftime("%Y/%m/%d"), p + "hidSearchsDate": d.strftime("%Y/%m/%d"), p + "hidStkId": fid,
                p + "hidLanguage": "TW", p + "hidProgramId": "Assets.aspx", p + "btnSearch": "查詢"}
        page = _retry(lambda: sess.post(url, data=data, timeout=40).text)
        cur = re.search(r'sDate"[^>]*value="([^"]*)"', page)
    if not (cur and cur.group(1) == d.strftime("%Y/%m/%d")):
        return None, {}
    holdings = {}
    for code, _name, shares in re.findall(r"<td[^>]*>\s*(\d{4,6}[A-Z]?)\s*</td>\s*<td[^>]*>\s*([^<]+?)\s*</td>\s*<td[^>]*>\s*([\d,]+)\s*</td>", page):
        holdings[code] = num(shares)
    return d.isoformat(), holdings


def fetch_cathay(fid, d):
    h = {"Origin": "https://www.cathaysite.com.tw", "Referer": "https://www.cathaysite.com.tw/"}
    j = _retry(lambda: S.get("https://cwapi.cathaysite.com.tw/api/ETF/GetETFDetailStockList",
                             params={"FundCode": fid, "SearchDate": d.strftime("%Y/%m/%d")}, headers=h, timeout=40).json())
    rows = j.get("result") or []
    if not rows:
        return None, {}
    return d.isoformat(), {x["stockCode"].strip(): num(x["volumn"]) for x in rows}


def fetch_yuanta(fid, d):
    p = {"APIType": "ETFAPI", "CompanyName": "YUANTAFUNDS", "PageName": f"/tradeInfo/pcf/{fid}", "DeviceId": "null",
         "FuncId": "PCF/Daily", "ticker": fid, "ndate": d.strftime("%Y%m%d")}
    j = _retry(lambda: S.get("https://etfapi.yuantaetfs.com/ectranslation/api/bridge", params=p, timeout=40).json())
    tran = (j.get("PCF") or {}).get("trandate")
    if tran != d.strftime("%Y%m%d"):
        return None, {}
    return d.isoformat(), {x["code"].strip(): x["qty"] for x in (j.get("FundWeights") or {}).get("StockWeights") or []}


def fetch_nomura(fid, d):
    h = {"Origin": "https://www.nomurafunds.com.tw", "Referer": "https://www.nomurafunds.com.tw/ETFWEB/"}
    j = _retry(lambda: S.post("https://www.nomurafunds.com.tw/API/ETFAPI/api/Fund/GetFundAssets",
                              json={"FundID": fid, "SearchDate": d.isoformat()}, headers=h, timeout=40).json())
    data = (j.get("Entries") or {}).get("Data") or {}
    hold = ((data.get("FundAsset") or {}).get("NavDate") or "").replace("/", "-")
    table = next((t for t in data.get("Table") or [] if t.get("TableTitle") == "股票"), None)
    if hold != d.isoformat() or not table:
        return None, {}
    return hold, {r[0].strip(): num(r[2]) for r in table.get("Rows") or []}


FETCH = {"nomura": fetch_nomura, "uni": fetch_uni, "cap": fetch_cap, "ctbc": fetch_ctbc, "fh": fetch_fh, "fubon": fetch_fubon, "cathay": fetch_cathay, "yuanta": fetch_yuanta}


# ---------- 計算 ----------
def diff(prev, cur):
    out = {}
    for c in set(prev) | set(cur):
        a, b = prev.get(c) or 0, cur.get(c) or 0
        # 不到 1 張的零股變動不列入（四捨五入後是 0，容易誤會）
        if a != b and (round((b - a) / LOT) != 0 or a == 0 or b == 0):
            out[c] = {"before": round(a / LOT), "after": round(b / LOT), "chg": round((b - a) / LOT),
                      "status": "新增" if a == 0 else "出清" if b == 0 else ("加碼" if b > a else "減碼")}
    return out


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=5, help="趨勢表的交易日數")
    ap.add_argument("--out", default=os.path.join(ROOT, "data", "etf.json"))
    args = ap.parse_args()

    days = trading_days()
    if not days:
        sys.exit("取不到交易日")
    today = days[-1]
    names = stock_names(today)
    etf_names = dict(names)
    active, top30, month_from = last_month_top_active(days, etf_names)
    overseas = [c for c in active if OVERSEAS.search(etf_names.get(c, ""))]
    active = [c for c in active if c not in overseas]

    tracked = [{"code": c, "kind": "主動"} for c in active] + [{"code": c, "kind": "被動"} for c in PASSIVE]
    need = days[-(args.days + 1):]  # 最近 N+1 個交易日（N 天的變化）
    result = {"date": today.isoformat(), "dates": [iso(x) for x in need[1:]], "month_from": month_from,
              "top30": top30, "overseas_excluded": [{"code": c, "name": etf_names.get(c, c)} for c in overseas],
              "etfs": [], "errors": {}}

    for t in tracked:
        code = t["code"]
        info = {"code": code, "name": etf_names.get(code, code), "kind": t["kind"]}
        reg = REGISTRY.get(code)
        if not reg:
            info["error"] = "尚未支援此投信"
            result["etfs"].append(info)
            continue
        issuer, fid = reg
        info["issuer"] = ISSUER_NAME[issuer]
        # 主動式抓 N+1 天做趨勢；被動式只抓最近 2 天
        want = need if t["kind"] == "主動" else need[-2:]
        snaps = {}
        for d in want:
            try:
                hd, h = FETCH[issuer](fid, d)
                if hd and h:
                    snaps[hd] = h
            except Exception as e:  # noqa: BLE001
                result["errors"][f"{code}@{d}"] = f"{type(e).__name__}: {e}"
            time.sleep(0.3)
        info["dates_ok"] = sorted(snaps)
        if today.isoformat() not in snaps or len(snaps) < 2:
            info["error"] = f"只取得 {len(snaps)} 天持股（需要今天與前一交易日）"
            result["etfs"].append(info)
            continue
        ordered = sorted(snaps)
        cur, prev = snaps[ordered[-1]], snaps[ordered[-2]]
        info["prev_date"] = ordered[-2]
        info["holdings_count"] = len(cur)
        info["holdings"] = {c: round(v / LOT) for c, v in cur.items()}
        ch = diff(prev, cur)
        info["changes"] = sorted(
            [{"code": c, "name": names.get(c, c), **v, "big": abs(v["chg"]) >= BIG_LOTS} for c, v in ch.items()],
            key=lambda x: -abs(x["chg"]))
        info["daily"] = {ordered[i]: {c: v["chg"] for c, v in diff(snaps[ordered[i - 1]], snaps[ordered[i]]).items()}
                         for i in range(1, len(ordered))}
        result["etfs"].append(info)
        print(f"{code} {info['name']}：{len(ch)} 檔變動（{info['prev_date']} → {ordered[-1]}）")

    ok = [e for e in result["etfs"] if "changes" in e]
    act = [e for e in ok if e["kind"] == "主動"]

    # 共識榜（主動式）
    agg = {}
    for e in act:
        for c in e["changes"]:
            a = agg.setdefault(c["code"], {"code": c["code"], "name": c["name"], "buy": 0, "sell": 0, "net": 0, "by": {}})
            a["net"] += c["chg"]
            a["by"][e["code"]] = c["chg"]
            if c["chg"] > 0:
                a["buy"] += 1
            elif c["chg"] < 0:
                a["sell"] += 1
    for e in ok:  # 熱力圖也放被動式
        if e["kind"] == "被動":
            for c in e["changes"]:
                if c["code"] in agg:
                    agg[c["code"]]["by"][e["code"]] = c["chg"]
    rows = list(agg.values())
    result["consensus_buy"] = sorted([r for r in rows if r["net"] > 0], key=lambda r: (-r["buy"], -r["net"]))[:15]
    result["consensus_sell"] = sorted([r for r in rows if r["net"] < 0], key=lambda r: (-r["sell"], r["net"]))[:15]
    result["heatmap"] = sorted(rows, key=lambda r: -abs(r["net"]))[:25]

    # 近 N 日趨勢（主動式合計）
    trend = {}
    for e in act:
        for day, chs in (e.get("daily") or {}).items():
            for c, v in chs.items():
                trend.setdefault(c, {}).setdefault(day, 0)
                trend[c][day] += v
    trows = []
    for c, by_day in trend.items():
        seq = [by_day.get(day, 0) for day in result["dates"]]
        streak = 0
        sign = 1 if seq and seq[-1] > 0 else -1 if seq and seq[-1] < 0 else 0
        for v in reversed(seq):
            if sign and v * sign > 0:
                streak += 1
            else:
                break
        trows.append({"code": c, "name": names.get(c, c), "seq": seq, "total": sum(seq), "streak": streak * sign})
    result["trend"] = sorted(trows, key=lambda r: (-abs(r["streak"]), -abs(r["total"])))[:20]
    held = {c for e in ok for c in e.get("holdings", {})}
    result["names"] = {c: names.get(c, c) for c in held}

    os.makedirs(os.path.dirname(args.out), exist_ok=True)
    with open(args.out, "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    write_excel(result)
    pending = [e["code"] for e in result["etfs"] if e.get("error")]
    print(f"etf.json 已寫入：成功 {len(ok)} 檔、未取得 {pending}、錯誤 {len(result['errors'])} 筆")


def write_excel(r):
    try:
        from openpyxl import Workbook
        from openpyxl.styles import Alignment, Font, PatternFill
    except ImportError:
        print("[warn] 沒有安裝 openpyxl，略過 Excel", file=sys.stderr)
        return
    wb = Workbook()
    red, green = PatternFill("solid", fgColor="FDE2E2"), PatternFill("solid", fgColor="DDF2E7")
    bold = Font(bold=True)

    ws = wb.active
    ws.title = "共識榜"
    ws.append([f"主動式 ETF 共識榜 {r['date']}（張）"])
    ws.append(["方向", "代號", "名稱", "加碼檔數", "減碼檔數", "合計張數"])
    for label, rows in (("加碼", r.get("consensus_buy", [])), ("減碼", r.get("consensus_sell", []))):
        for x in rows:
            ws.append([label, x["code"], x["name"], x["buy"], x["sell"], x["net"]])

    ok = [e for e in r["etfs"] if "changes" in e]
    ws = wb.create_sheet("熱力圖")
    ws.append(["代號", "名稱"] + [f"{e['code']}{'（被動）' if e['kind'] == '被動' else ''}" for e in ok] + ["主動合計"])
    for x in r.get("heatmap", []):
        vals = [x["by"].get(e["code"]) for e in ok]
        ws.append([x["code"], x["name"]] + vals + [x["net"]])
        for i, v in enumerate(vals, start=3):
            if v:
                ws.cell(row=ws.max_row, column=i).fill = red if v > 0 else green

    ws = wb.create_sheet("近5日趨勢")
    ws.append(["代號", "名稱"] + r["dates"] + ["合計", "連續天數"])
    for x in r.get("trend", []):
        ws.append([x["code"], x["name"]] + x["seq"] + [x["total"], x["streak"]])

    for e in r["etfs"]:
        ws = wb.create_sheet(e["code"][:31])
        ws.append([f"{e['code']} {e['name']}（{e.get('issuer', '')}，{e['kind']}）"])
        if e.get("error"):
            ws.append([e["error"]])
            continue
        ws.append([f"比對 {e['prev_date']} → {r['date']}，持股 {e['holdings_count']} 檔"])
        ws.append(["代號", "名稱", "前日張數", "今日張數", "增減", "狀態"])
        for c in e["changes"]:
            ws.append([c["code"], c["name"], c["before"], c["after"], c["chg"], c["status"]])
            if c["big"]:
                for col in range(1, 7):
                    ws.cell(row=ws.max_row, column=col).font = bold
        ws.append([])
        ws.append(["完整持股", "名稱", "今日張數"])
        for c, lots in sorted(e["holdings"].items(), key=lambda kv: -kv[1]):
            ws.append([c, r.get("names", {}).get(c, ""), lots])
    for sheet in wb.worksheets:
        sheet.column_dimensions["B"].width = 16
        sheet["A1"].font = Font(bold=True, size=13)
        sheet["A1"].alignment = Alignment(horizontal="left")
    os.makedirs(os.path.join(ROOT, "out"), exist_ok=True)
    path = os.path.join(ROOT, "out", f"ETF持股變化_{r['date']}.xlsx")
    wb.save(path)
    with open(os.path.join(ROOT, "out", "attachments.json"), "w", encoding="utf-8") as f:
        json.dump([path], f, ensure_ascii=False)
    print(f"Excel 已寫入 {path}")


if __name__ == "__main__":
    main()
