"""把 data/*.json 套進 templates/，用無頭瀏覽器輸出 PNG 到 out/。

用法：
  python scripts/render.py --mode daily      # 每日精華圖 + 本週財報表
  python scripts/render.py --mode saturday   # 本週結論 + 下週前瞻 + 下週財報表
"""
import argparse
import json
import os
import sys

from jinja2 import Environment, FileSystemLoader, Undefined, select_autoescape
from playwright.sync_api import sync_playwright

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DATA = os.path.join(ROOT, "data")
OUT = os.path.join(ROOT, "out")


def _missing(v):
    return v is None or isinstance(v, Undefined)


def fmt(v, digits=2):
    if _missing(v):
        return "—"
    try:
        return f"{float(v):,.{int(digits)}f}"
    except (TypeError, ValueError):
        return str(v)


def cls(v):
    if _missing(v):
        return "flat"
    try:
        f = float(v)
    except (TypeError, ValueError):
        return "flat"
    return "up" if f > 0 else "down" if f < 0 else "flat"


def load(name, required=True):
    path = os.path.join(DATA, name)
    if not os.path.exists(path):
        if required:
            sys.exit(f"缺少 {path}")
        return {}
    with open(path, encoding="utf-8") as f:
        return json.load(f)


def chart_data(m):
    """把淨流動性（週三）與 SPY/SOXX 週線對齊：每個週三取當天或之前最近的收盤價。"""
    nl = (m.get("net_liquidity") or {}).get("series") or []
    weekly = m.get("weekly") or {}

    def asof(series, date):
        vals = [x["close"] for x in series if x["date"] <= date]
        return vals[-1] if vals else None

    labels = [x["date"][5:].replace("-", "/") for x in nl]
    return {
        "labels": labels,
        "nl": [x["value"] for x in nl],
        "spy": [asof(weekly.get("SPY", []), x["date"]) for x in nl],
        "soxx": [asof(weekly.get("SOXX", []), x["date"]) for x in nl],
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--mode", choices=["daily", "saturday", "tw"], required=True)
    ap.add_argument("--scale", type=float, default=1.5, help="輸出解析度倍率")
    args = ap.parse_args()

    tw_mode = args.mode == "tw"
    m = load("market.json", required=not tw_mode)
    e = load("earnings.json", required=not tw_mode)
    c = load("tw_content.json" if tw_mode else "content.json")
    t = load("tw.json", required=tw_mode)
    f = load("etf.json", required=False)
    if tw_mode:  # 台股日報的產生時間以台股資料為準
        m = {**(m or {}), "generated_at_utc": t.get("generated_at_utc", "")}

    env = Environment(loader=FileSystemLoader(os.path.join(ROOT, "templates")), autoescape=select_autoescape(["html"]))
    env.filters["fmt"] = fmt
    env.filters["cls"] = cls

    if tw_mode:
        pages = [("1_tw_market", "tw_market.html"), ("2_tw", "tw.html"), ("3_tw_etf", "tw_etf.html"),
                 ("4_tw_etf_detail", "tw_etf_detail.html"), ("5_tw_events", "tw_events.html")]
        if not f:
            pages = [p for p in pages if not p[1].startswith("tw_etf")]
            print("[warn] 沒有 data/etf.json，略過 ETF 持股圖", file=sys.stderr)
        week_label = ""
    elif args.mode == "daily":
        pages = [("1_daily", "daily.html"), ("2_tw", "tw.html"), ("3_earnings", "earnings.html")]
        week_label = "本週"
    else:
        pages = [("1_week_review", "week_review.html"), ("2_week_outlook", "week_outlook.html"), ("3_tw", "tw.html"), ("4_earnings", "earnings.html")]
        week_label = "下週"
    if not t:
        pages = [p for p in pages if p[1] != "tw.html"]
        print("[warn] 沒有 data/tw.json，略過台股籌碼圖", file=sys.stderr)

    ctx = {"m": m, "e": e, "c": c, "t": t, "f": f, "week_label": week_label, "chart": chart_data(m)}
    os.makedirs(OUT, exist_ok=True)
    outputs = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page(viewport={"width": 1600, "height": 900}, device_scale_factor=args.scale)
        for name, tpl in pages:
            html_path = os.path.join(OUT, f"{name}.html")
            with open(html_path, "w", encoding="utf-8") as f:
                f.write(env.get_template(tpl).render(**ctx))
            page.goto("file:///" + html_path.replace("\\", "/").lstrip("/"), wait_until="networkidle", timeout=90000)
            page.evaluate("document.fonts.ready")
            page.wait_for_timeout(800)
            png = os.path.join(OUT, f"{name}.png")
            page.screenshot(path=png, full_page=True)
            outputs.append(png)
            print(f"已輸出 {png}")
        browser.close()
    with open(os.path.join(OUT, "images.json"), "w", encoding="utf-8") as f:
        json.dump(outputs, f, ensure_ascii=False)


if __name__ == "__main__":
    main()
