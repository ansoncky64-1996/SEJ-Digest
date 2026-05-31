#!/usr/bin/env python3
"""
SEJ SEO 專欄 → AI 加工 → 直接 Email 俾同事(Gmail SMTP 版)
------------------------------------------------------------
流程:讀 SEJ SEO RSS → 揀出未發過嘅新文 → 經 OpenRouter 生成中英對照重點
     → 砌一封有公司 branding 嘅 HTML email → 經 Gmail SMTP 發俾收件人。
唔再需要 Brevo / RSS feed。去重記錄存喺 docs/state.json。

測試:喺 GitHub Actions 撳 Run workflow,將「測試模式」剔 true,
     佢會將最近 3 篇加工後只寄俾你自己,唔影響正式去重記錄。
"""

import os
import re
import ssl
import json
import html
import smtplib
import datetime as dt
from pathlib import Path
from email.message import EmailMessage

import feedparser
import trafilatura
from openai import OpenAI

# ---------- 基本設定 ----------
SOURCE_FEED = os.environ.get("SOURCE_FEED", "https://www.searchenginejournal.com/category/seo/feed/")
MODEL = os.environ.get("MODEL", "deepseek/deepseek-v4-flash")  # ← 換返你之前用嘅 DeepSeek V4 ID
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", "10"))  # 單次最多加工幾多篇(防爆)
SEEN_CAP = 500  # state.json 記住幾多條舊連結

STATE_PATH = Path(os.environ.get("STATE_PATH", "docs/state.json"))
HKT = dt.timezone(dt.timedelta(hours=8))

# ---------- Email / Branding 設定(由環境變數提供)----------
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")          # 你個 Gmail(寄件人)
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "") # Gmail app password(16 位)
RECIPIENTS = [e.strip() for e in os.environ.get("RECIPIENTS", "").split(",") if e.strip()]
BRAND_NAME = os.environ.get("BRAND_NAME", "SEO 每日重點")
BRAND_COLOR = os.environ.get("BRAND_COLOR", "#1f4e79")
LOGO_URL = os.environ.get("LOGO_URL", "")                    # 可選:logo 圖片網址
TEST_SEND = os.environ.get("TEST_SEND", "").lower() in ("1", "true", "yes")

client = OpenAI(base_url="https://openrouter.ai/api/v1", api_key=os.environ.get("OPENROUTER_API_KEY"))

PROMPT = """你係一個為香港 SEO 團隊服務嘅新聞編輯。以下係一篇 Search Engine Journal 嘅 SEO 文章。

請將佢濃縮成「中英對照重點」,規格如下:
- 先英文版,後繁體中文版,兩個版本內容互相對應
- 每個版本 3 至 4 個 bullet point
- 繁體中文部分總字數約 200–300 字,用香港讀者睇得明嘅書面語
- 聚焦「對 SEO 工作有咩實際啟示 / action」,唔好淨係複述標題
- 唔好捏造文中冇出現過嘅數據或結論

只輸出以下 HTML 片段,唔好有任何其他文字、唔好用 markdown code fence:

<p style="margin:0 0 6px;font-weight:700;">🔑 Key Takeaways</p>
<ul style="margin:0 0 12px;padding-left:20px;">
<li>...</li>
</ul>
<p style="margin:0 0 6px;font-weight:700;">🔑 重點(繁體中文)</p>
<ul style="margin:0;padding-left:20px;">
<li>...</li>
</ul>

文章標題:{title}
原文連結:{link}

文章內容:
{body}
"""


def strip_html(s: str) -> str:
    s = re.sub(r"(?is)<(script|style).*?>.*?</\1>", " ", s)
    s = re.sub(r"(?s)<[^>]+>", " ", s)
    return re.sub(r"\s+", " ", html.unescape(s)).strip()


def load_seen() -> list:
    """讀去重記錄;兼容舊格式 {'items': {...}} 同新格式 {'seen': [...]}。"""
    if not STATE_PATH.exists():
        return []
    try:
        data = json.loads(STATE_PATH.read_text(encoding="utf-8"))
    except Exception:
        return []
    if isinstance(data, dict) and "seen" in data:
        return list(data["seen"])
    if isinstance(data, dict) and "items" in data:  # 由舊版本遷移
        return list(data["items"].keys())
    return []


def save_seen(seen: list) -> None:
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps({"seen": seen[-SEEN_CAP:]}, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_text(entry) -> str:
    raw = ""
    if entry.get("content"):
        raw = entry["content"][0].get("value", "") or ""
    if not raw:
        raw = entry.get("summary", "") or ""
    text = None
    if raw:
        try:
            text = trafilatura.extract(raw)
        except Exception:
            text = None
        if not text:
            text = strip_html(raw)
    if (not text or len(text) < 400) and entry.get("link"):
        try:
            downloaded = trafilatura.fetch_url(entry["link"])
            if downloaded:
                full = trafilatura.extract(downloaded)
                if full and len(full) > len(text or ""):
                    text = full
        except Exception:
            pass
    return (text or entry.get("title", ""))[:8000]


def summarise(title: str, link: str, body: str) -> str:
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=1200,
        messages=[{"role": "user", "content": PROMPT.format(title=title, link=link, body=body)}],
    )
    out = (resp.choices[0].message.content or "").strip()
    if out.startswith("```"):
        out = out.strip("`")
        out = out.split("\n", 1)[1] if "\n" in out else out
    return out


def build_email_html(items: list) -> str:
    today = dt.datetime.now(HKT).strftime("%Y-%m-%d")
    if LOGO_URL:
        brand = f'<img src="{html.escape(LOGO_URL)}" alt="{html.escape(BRAND_NAME)}" height="32" style="display:block;border:0;">'
    else:
        brand = f'<span style="font-size:20px;font-weight:800;color:#ffffff;letter-spacing:.3px;">{html.escape(BRAND_NAME)}</span>'

    cards = []
    for it in items:
        cards.append(f'''
      <tr><td style="padding:22px 28px;border-bottom:1px solid #eeeeee;">
        <a href="{html.escape(it['link'])}" style="font-size:17px;font-weight:700;color:#1a1a1a;text-decoration:none;line-height:1.4;">{html.escape(it['title'])}</a>
        <div style="margin-top:12px;font-size:14px;color:#333333;line-height:1.65;">{it['summary_html']}</div>
        <a href="{html.escape(it['link'])}" style="display:inline-block;margin-top:12px;font-size:13px;font-weight:600;color:{BRAND_COLOR};text-decoration:none;">閱讀原文 →</a>
      </td></tr>''')

    return f'''<!DOCTYPE html><html><body style="margin:0;background:#f4f5f7;">
  <table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:#f4f5f7;padding:24px 0;">
    <tr><td align="center">
      <table role="presentation" width="600" cellpadding="0" cellspacing="0" style="max-width:600px;width:100%;background:#ffffff;border-radius:10px;overflow:hidden;font-family:-apple-system,'Segoe UI',Roboto,'Helvetica Neue',Arial,'PingFang HK','Microsoft JhengHei',sans-serif;">
        <tr><td style="background:{BRAND_COLOR};padding:18px 28px;">{brand}
          <div style="margin-top:4px;font-size:13px;color:#dfe7f1;">SEO 每日重點 · {today}</div>
        </td></tr>
        {''.join(cards)}
        <tr><td style="padding:18px 28px;background:#fafafa;font-size:12px;color:#888888;line-height:1.6;">
          內容由 Search Engine Journal 文章經 AI 整理重點,版權歸原作者,請按標題閱讀原文。<br>呢封係團隊內部 SEO 摘要。
        </td></tr>
      </table>
    </td></tr>
  </table>
</body></html>'''


def send_email(subject: str, html_body: str, recipients: list) -> None:
    if not (GMAIL_ADDRESS and GMAIL_APP_PASSWORD):
        raise SystemExit("未設定 GMAIL_ADDRESS / GMAIL_APP_PASSWORD,無法發信。")
    if not recipients:
        raise SystemExit("收件人名單為空。")
    msg = EmailMessage()
    msg["Subject"] = subject
    msg["From"] = f"{BRAND_NAME} <{GMAIL_ADDRESS}>"
    msg["To"] = GMAIL_ADDRESS                       # 自己做 To
    msg["Bcc"] = ", ".join(recipients)              # 收件人放 Bcc,互相睇唔到 email
    msg.set_content("呢封 email 需要支援 HTML 嘅郵件程式先睇到完整內容。")
    msg.add_alternative(html_body, subtype="html")
    with smtplib.SMTP_SSL("smtp.gmail.com", 465, context=ssl.create_default_context()) as server:
        server.login(GMAIL_ADDRESS, GMAIL_APP_PASSWORD)
        server.send_message(msg)


def main():
    parsed = feedparser.parse(SOURCE_FEED)
    if parsed.bozo and not parsed.entries:
        raise SystemExit(f"無法讀取來源 feed: {SOURCE_FEED}")

    # ----- 測試模式:加工最近 3 篇,只寄俾自己,唔改去重記錄 -----
    if TEST_SEND:
        sample = parsed.entries[:3]
        items = []
        for e in sample:
            print(f"[測試] 加工:{e.get('title')}")
            items.append({"title": e.get("title", "(無標題)"), "link": e.get("link", ""),
                          "summary_html": summarise(e.get("title", ""), e.get("link", ""), extract_text(e))})
        today = dt.datetime.now(HKT).strftime("%Y-%m-%d")
        send_email(f"[測試] {BRAND_NAME}|SEO 每日重點 {today}", build_email_html(items), [GMAIL_ADDRESS])
        print(f"[測試] 已寄 {len(items)} 篇樣本俾 {GMAIL_ADDRESS}")
        return

    # ----- 正式模式:只處理未發過嘅新文 -----
    seen = load_seen()
    seen_set = set(seen)
    new_items = []
    for entry in parsed.entries:
        link = entry.get("link")
        if not link or link in seen_set:
            continue
        if len(new_items) >= MAX_NEW_PER_RUN:
            break
        title = entry.get("title", "(無標題)")
        print(f"加工新文章:{title}")
        try:
            summary = summarise(title, link, extract_text(entry))
        except Exception as ex:
            print(f"  加工失敗,今次跳過:{ex}")
            continue
        new_items.append({"title": title, "link": link, "summary_html": summary})
        seen.append(link)

    if not new_items:
        print("今日冇新文章,唔發送。")
        save_seen(seen)  # 不變,但確保檔案格式正確
        return

    today = dt.datetime.now(HKT).strftime("%Y-%m-%d")
    send_email(f"{BRAND_NAME}|SEO 每日重點 {today}", build_email_html(new_items), RECIPIENTS)
    save_seen(seen)
    print(f"完成:已發送 {len(new_items)} 篇新文章俾 {len(RECIPIENTS)} 位收件人。")


if __name__ == "__main__":
    main()
