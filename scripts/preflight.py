"""寄信前品質檢查。send_email.py 寄信前會自動執行，任何一項「必過」沒通過就不寄。

檢查項目：
1. 美股數據是收盤價（不是盤中）
2. 前一個美股交易日盤後公布財報的重要公司，晨報裡有寫到實際結果
3. 交叉查核至少 20 項（out/verify.md）
4. 台指期夜盤是昨晚那一場
5. 圖片都有產生、台股籌碼資料沒有錯誤（只提醒，不擋）

用法：python scripts/preflight.py   （結果寫在 out/preflight.json，exit code 1 = 未通過）
"""
import datetime as dt
import json
import os
import re
import sys
from zoneinfo import ZoneInfo

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "out")
ET = ZoneInfo("America/New_York")
MIN_CHECKS = 20
BIG_CAP = 100e9  # 市值 1,000 億美元以上的盤後財報公司，晨報一定要寫到


def load(path):
    try:
        with open(path, encoding="utf-8") as f:
            return json.load(f) if path.endswith(".json") else f.read()
    except FileNotFoundError:
        return None


def last_us_close(now_et):
    """最近一次已收盤的美股交易日（不含國定假日判斷，週末會往前推）。"""
    d = now_et.date()
    if now_et.time() < dt.time(16, 15):
        d -= dt.timedelta(days=1)
    while d.weekday() >= 5:
        d -= dt.timedelta(days=1)
    return d


def run_tw():
    """台股日報（21:30）的檢查：資料要是今天的、融資已公布、查核滿 20 項。"""
    now = dt.datetime.now(ZoneInfo("Asia/Taipei"))
    today = (now.date() if now.hour >= 15 else now.date() - dt.timedelta(days=1)).isoformat()
    t = load(os.path.join(DATA, "tw.json")) or {}
    report = load(os.path.join(OUT, "report.md")) or ""
    verify = load(os.path.join(OUT, "verify.md")) or ""
    images = load(os.path.join(OUT, "images.json")) or []
    checks = []

    def add(name, ok, detail, required=True):
        checks.append({"name": name, "ok": bool(ok), "required": required, "detail": detail})

    add("台股資料為今天", t.get("date") == today and not t.get("holiday"), f"tw.json 資料日 {t.get('date')}，今天 {today}")
    add("融資融券為今天", bool((t.get("margin") or {}).get("fin_lots")) and t.get("date") == today, "融資資料日期與今天一致" if t.get("date") == today else "融資資料不是今天的")
    rows = [ln for ln in verify.splitlines() if ln.strip().startswith("|") and re.search(r"✅|✏️|⚠️", ln)]
    add("交叉查核至少 20 項", len(rows) >= MIN_CHECKS, f"out/verify.md 有 {len(rows)} 項查核紀錄")
    add("圖片已產生", len(images) >= 3 and all(os.path.exists(p) for p in images), f"{len(images)} 張")
    etf = load(os.path.join(DATA, "etf.json")) or {}
    etf_ok = [x for x in etf.get("etfs", []) if "changes" in x]
    add("ETF 持股為今天", etf.get("date") == today and len(etf_ok) >= 5,
        f"etf.json 資料日 {etf.get('date')}，成功 {len(etf_ok)} 檔", required=False)
    missing = [k for k in ("overview", "options", "events", "institutional") if k not in t]
    add("延伸資料齊全", not missing, f"缺少 {missing or '無'}", required=False)
    add("台股資料無錯誤", not t.get("errors"), f"errors={list((t.get('errors') or {}).keys())}", required=False)
    add("報告有資料查核小節", "資料查核" in report, "report.md 最後要有「資料查核」小節", required=False)
    checks.append(simplified_check(report, *_content_texts("tw_content.json")))
    return _finish(checks)


# 常見的「只有簡體才有」的字（繁體不會出現）
# 注意：只能放繁體不會用到的字（例如「跌」「硬」繁簡同形，不能放進來）
SIMPLIFIED = set("这说为国们发时会对进过还经资业产数质际贸汇银证币场长东车门问间题实现无与样买卖级师网络电脑软设计统历岁观视读写书图团环险该应边关开来见总点线显弹压涨额亿万号处转运输气热让认识议论")


def simplified_check(*texts):
    found = sorted({ch for t in texts for ch in (t or "") if ch in SIMPLIFIED})
    return {"name": "沒有簡體字", "ok": not found, "required": True,
            "detail": f"發現簡體字：{''.join(found[:20])}" if found else "未發現簡體字"}


def _content_texts(*names):
    out = []
    for n in names:
        v = load(os.path.join(DATA, n))
        out.append(json.dumps(v, ensure_ascii=False) if v else "")
    return out


def _finish(checks):
    now_et = dt.datetime.now(ET)
    passed = all(c["ok"] for c in checks if c["required"])
    result = {"passed": passed, "checked_at_et": now_et.strftime("%Y-%m-%d %H:%M"), "checks": checks}
    os.makedirs(OUT, exist_ok=True)
    with open(os.path.join(OUT, "preflight.json"), "w", encoding="utf-8") as f:
        json.dump(result, f, ensure_ascii=False, indent=2)
    return result


def run(mode="brief"):
    if mode == "tw":
        return run_tw()
    now_et = dt.datetime.now(ET)
    m = load(os.path.join(DATA, "market.json")) or {}
    e = load(os.path.join(DATA, "earnings.json")) or {}
    t = load(os.path.join(DATA, "tw.json")) or {}
    report = load(os.path.join(OUT, "report.md")) or ""
    verify = load(os.path.join(OUT, "verify.md")) or ""
    images = load(os.path.join(OUT, "images.json")) or []

    checks = []

    def add(name, ok, detail, required=True):
        checks.append({"name": name, "ok": bool(ok), "required": required, "detail": detail})

    # 1. 美股收盤
    expect = last_us_close(now_et)
    spx = (m.get("prices") or {}).get("^GSPC") or {}
    gen = m.get("generated_at_utc", "")
    gen_et = None
    try:
        gen_et = dt.datetime.strptime(gen, "%Y-%m-%d %H:%M").replace(tzinfo=dt.timezone.utc).astimezone(ET)
        after_close = gen_et.date() > dt.date.fromisoformat(spx.get("date", "1900-01-01")) or gen_et.time() >= dt.time(16, 15)
    except ValueError:
        after_close = False
    add(
        "美股為收盤價",
        spx.get("date") == expect.isoformat() and after_close,
        f"S&P 500 資料日期 {spx.get('date')}，應為 {expect}；抓取時間（美東）{gen_et:%m/%d %H:%M}" if gen_et else "market.json 沒有產生時間",
    )

    # 2. 盤後財報結果
    missing, covered = [], []
    for day in e.get("days") or []:
        if day.get("date") != expect.isoformat():
            continue
        for c in day.get("amc") or []:
            if c.get("market_cap", 0) >= BIG_CAP:
                hit = re.search(rf"\b{re.escape(c['symbol'])}\b", report) or (c.get("name", "")[:6] and c["name"][:6] in report)
                (covered if hit else missing).append(c["symbol"])
    has_result_words = bool(re.search(r"EPS|每股盈餘", report)) and "營收" in report
    if covered or missing:
        add("盤後財報有寫實際結果", not missing and has_result_words,
            f"應涵蓋 {covered + missing}；缺少 {missing or '無'}；{'有' if has_result_words else '沒有'} EPS／營收數字")
    else:
        add("盤後財報有寫實際結果", True, f"{expect} 沒有市值 1,000 億美元以上的盤後財報公司", required=False)

    # 3. 交叉查核 >= 20 項
    rows = [ln for ln in verify.splitlines() if ln.strip().startswith("|") and re.search(r"✅|✏️|⚠️", ln)]
    add("交叉查核至少 20 項", len(rows) >= MIN_CHECKS, f"out/verify.md 有 {len(rows)} 項查核紀錄")

    # 4. 台指期夜盤
    txn = t.get("tx_night") or {}
    add("台指期夜盤為昨晚", txn.get("is_last_night") is True,
        f"夜盤日期 {txn.get('night_evening')}，籌碼資料日 {t.get('date')}")

    # 5. 提醒項目
    add("圖片已產生", len(images) >= 3 and all(os.path.exists(p) for p in images), f"{len(images)} 張", required=False)
    add("台股籌碼無錯誤", not t.get("errors"), f"errors={list((t.get('errors') or {}).keys())}", required=False)
    add("報告有資料查核小節", "資料查核" in report, "report.md 最後要有「資料查核」小節", required=False)
    checks.append(simplified_check(report, *_content_texts("content.json")))

    return _finish(checks)


def main():
    mode = "tw" if "--mode" in sys.argv and sys.argv[sys.argv.index("--mode") + 1] == "tw" else "brief"
    r = run(mode)
    for c in r["checks"]:
        mark = "✅" if c["ok"] else ("❌" if c["required"] else "⚠️")
        print(f"{mark} {c['name']}：{c['detail']}")
    print("品質檢查：" + ("通過" if r["passed"] else "未通過"))
    sys.exit(0 if r["passed"] else 1)


if __name__ == "__main__":
    main()
