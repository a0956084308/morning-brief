"""寄出晨報：圖片嵌在信件內文，下方接完整晨報（Markdown 轉 HTML）。

需要環境變數：
  GMAIL_USER          寄件 Gmail 帳號
  GMAIL_APP_PASSWORD  Gmail 應用程式密碼（16 碼）
  MAIL_TO             收件人，可省略（預設寄給 GMAIL_USER），多人用逗號分隔

用法：
  python scripts/send_email.py --subject "國際財經晨報 11/03（週二）" --report out/report.md
  python scripts/send_email.py ... --dry-run   # 只產生 out/email.html 預覽，不寄出
"""
import argparse
import json
import os
import smtplib
import sys
from email.mime.image import MIMEImage
from email.mime.multipart import MIMEMultipart
from email.mime.text import MIMEText

import markdown

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
OUT = os.path.join(ROOT, "out")

STYLE = """
body { font-family: 'Noto Sans TC','Microsoft JhengHei',sans-serif; color:#1b2430; line-height:1.6; max-width:1000px; margin:0 auto; padding:12px; }
img.brief { width:100%; max-width:1000px; border-radius:8px; margin:0 0 14px; border:1px solid #dfe4ea; }
h1 { font-size:24px; } h2,h3 { font-size:19px; border-left:5px solid #c8102e; padding-left:8px; margin-top:24px; }
table { border-collapse:collapse; width:100%; font-size:14px; margin:8px 0; }
th,td { border:1px solid #dfe4ea; padding:6px 8px; text-align:left; vertical-align:top; }
th { background:#f3f5f8; }
blockquote { color:#5c6878; border-left:4px solid #dfe4ea; margin:8px 0; padding-left:10px; }
.note { color:#5c6878; font-size:12px; margin-top:24px; }
"""


PAGE_NAMES = {
    "daily": "每日精華",
    "tw": "台股籌碼",
    "earnings": "SP500財報時間表",
    "week_review": "本週市場結論",
    "week_outlook": "下週前瞻",
}


def attachment_name(path):
    """1_daily.png → 2026-11-03_1_每日精華.png，方便下載後辨識。"""
    import datetime as dt
    from zoneinfo import ZoneInfo

    stem = os.path.splitext(os.path.basename(path))[0]
    idx, _, key = stem.partition("_")
    today = dt.datetime.now(ZoneInfo("Asia/Taipei")).strftime("%Y-%m-%d")
    return f"{today}_{idx}_{PAGE_NAMES.get(key, key)}.png"


def send_resend(subject, html, text, images):
    """透過 Resend HTTPS API 寄信（雲端環境擋 SMTP 時使用）。

    免費方案沒有自己的網域時，寄件者固定用 onboarding@resend.dev，
    且只能寄到註冊 Resend 的那個信箱。
    """
    import base64

    import requests

    to = [x.strip() for x in (os.environ.get("MAIL_TO") or "a0956084308@gmail.com").split(",") if x.strip()]
    sender = os.environ.get("MAIL_FROM") or "國際財經晨報 <onboarding@resend.dev>"
    attachments = []
    for i, p in enumerate(images):
        with open(p, "rb") as f:
            b64 = base64.b64encode(f.read()).decode()
        name = attachment_name(p)
        # 內文顯示用（inline）
        attachments.append({"filename": f"inline_{name}", "content": b64, "content_type": "image/png", "content_id": f"img{i}"})
        # 一般附件：可以點開放大、下載
        attachments.append({"filename": name, "content": b64, "content_type": "image/png"})
    r = requests.post(
        "https://api.resend.com/emails",
        headers={"Authorization": f"Bearer {os.environ['RESEND_API_KEY']}", "Content-Type": "application/json"},
        json={"from": sender, "to": to, "subject": subject, "html": html, "text": text, "attachments": attachments},
        timeout=120,
    )
    if r.status_code >= 300:
        sys.exit(f"Resend 寄信失敗 HTTP {r.status_code}：{r.text[:500]}")
    print(f"已寄出（Resend）：{subject} → {', '.join(to)}（圖片 {len(images)} 張）id={r.json().get('id')}")


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--subject", required=True)
    ap.add_argument("--report", default=os.path.join(OUT, "report.md"))
    ap.add_argument("--images", default=os.path.join(OUT, "images.json"), help="render.py 輸出的圖片清單")
    ap.add_argument("--dry-run", action="store_true")
    ap.add_argument("--allow-warnings", action="store_true", help="品質檢查未通過仍寄出（信件最上方會列出未通過項目）")
    args = ap.parse_args()

    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import preflight

    pf = preflight.run()
    failed = [c for c in pf["checks"] if c["required"] and not c["ok"]]
    for c in pf["checks"]:
        print(("✅" if c["ok"] else "❌" if c["required"] else "⚠️") + f" {c['name']}：{c['detail']}")
    if failed and not args.allow_warnings and not args.dry_run:
        sys.exit("品質檢查未通過，沒有寄出。請修正上面標 ❌ 的項目後重新寄送；"
                 "真的無法修正且快到寄送期限時，才加 --allow-warnings 寄出。")

    with open(args.report, encoding="utf-8") as f:
        report_md = f.read()
    images = []
    if os.path.exists(args.images):
        with open(args.images, encoding="utf-8") as f:
            images = [p for p in json.load(f) if os.path.exists(p)]

    img_tags = "".join(f'<img class="brief" src="cid:img{i}" alt="晨報圖片 {i + 1}">' for i in range(len(images)))
    if images:
        img_tags = "<p class='note' style='margin-top:0'>💡 想放大或下載圖片，請點信件最下方的附件。</p>" + img_tags
    if failed:
        items = "".join(f"<li>{c['name']}：{c['detail']}</li>" for c in failed)
        img_tags = (
            "<div style='border:2px solid #e2711d;border-radius:8px;padding:8px 12px;margin-bottom:12px'>"
            f"<b style='color:#e2711d'>⚠ 寄信前品質檢查有 {len(failed)} 項未通過，請留意：</b><ul>{items}</ul></div>"
        ) + img_tags
    else:
        n = sum(1 for c in pf["checks"] if c["ok"])
        img_tags = f"<p class='note' style='margin-top:0'>✅ 寄信前品質檢查通過（{n}/{len(pf['checks'])} 項）</p>" + img_tags
    body = markdown.markdown(report_md, extensions=["tables", "sane_lists"])
    html = (
        f"<html><head><meta charset='utf-8'><style>{STYLE}</style></head><body>"
        f"{img_tags}{body}"
        "<p class='note'>本信由自動排程產生，內容僅供參考，不構成投資建議。</p></body></html>"
    )

    if args.dry_run:
        preview = html
        for i, p in enumerate(images):
            preview = preview.replace(f"cid:img{i}", "file:///" + p.replace("\\", "/").lstrip("/"))
        path = os.path.join(OUT, "email.html")
        with open(path, "w", encoding="utf-8") as f:
            f.write(preview)
        print(f"預覽已寫入 {path}（未寄出）")
        return

    if os.environ.get("RESEND_API_KEY"):
        send_resend(args.subject, html, report_md, images)
        return

    user = os.environ.get("GMAIL_USER")
    pw = (os.environ.get("GMAIL_APP_PASSWORD") or "").replace(" ", "")
    if not user or not pw:
        sys.exit("缺少環境變數 RESEND_API_KEY（或 GMAIL_USER＋GMAIL_APP_PASSWORD），請到雲端環境設定填入")
    to = [x.strip() for x in (os.environ.get("MAIL_TO") or user).split(",") if x.strip()]

    msg = MIMEMultipart("related")
    msg["Subject"] = args.subject
    msg["From"] = f"國際財經晨報 <{user}>"
    msg["To"] = ", ".join(to)
    alt = MIMEMultipart("alternative")
    alt.attach(MIMEText(report_md, "plain", "utf-8"))
    alt.attach(MIMEText(html, "html", "utf-8"))
    msg.attach(alt)
    for i, p in enumerate(images):
        with open(p, "rb") as f:
            part = MIMEImage(f.read(), _subtype="png")
        part.add_header("Content-ID", f"<img{i}>")
        part.add_header("Content-Disposition", "inline", filename=os.path.basename(p))
        msg.attach(part)

    with smtplib.SMTP_SSL("smtp.gmail.com", 465, timeout=60) as s:
        s.login(user, pw)
        s.send_message(msg)
    print(f"已寄出：{args.subject} → {', '.join(to)}（圖片 {len(images)} 張）")


if __name__ == "__main__":
    main()
