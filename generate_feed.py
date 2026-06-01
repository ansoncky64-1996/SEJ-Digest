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
# 多個來源:用逗號分隔。想加站就喺呢度(或 workflow 嘅 SOURCE_FEEDS)加多條 feed URL。
SOURCE_FEEDS = [u.strip() for u in os.environ.get(
    "SOURCE_FEEDS",
    "https://www.searchenginejournal.com/category/seo/feed/,https://rss.app/feeds/UEkjjet8qs4Vw8BA.xml",
).split(",") if u.strip()]
MODEL = os.environ.get("MODEL", "deepseek/deepseek-v4-flash")  # ← 換返你之前用嘅 DeepSeek V4 ID
# 只接受呢啲網域嘅文章(隔走 RSS.app 夾雜嘅 webinar/廣告等雜連結)。逗號分隔;留空 = 全部接受。
ALLOWED_DOMAINS = [d.strip().lower() for d in os.environ.get(
    "ALLOWED_DOMAINS",
    "searchenginejournal.com,searchengineland.com",
).split(",") if d.strip()]
MAX_NEW_PER_RUN = int(os.environ.get("MAX_NEW_PER_RUN", "10"))  # 單次最多加工幾多篇(防爆)
SEEN_CAP = 500  # state.json 記住幾多條舊連結

STATE_PATH = Path(os.environ.get("STATE_PATH", "docs/state.json"))
HKT = dt.timezone(dt.timedelta(hours=8))

# ---------- Email / Branding 設定(由環境變數提供)----------
GMAIL_ADDRESS = os.environ.get("GMAIL_ADDRESS", "")          # 你個 Gmail(寄件人)
GMAIL_APP_PASSWORD = os.environ.get("GMAIL_APP_PASSWORD", "") # Gmail app password(16 位)
RECIPIENTS = [e.strip() for e in os.environ.get("RECIPIENTS", "").split(",") if e.strip()]
BRAND_NAME = os.environ.get("BRAND_NAME", "Digital Zoo")
BRAND_COLOR = os.environ.get("BRAND_COLOR", "#1f4e79")
LOGO_URL = os.environ.get("LOGO_URL", "https://digitalzoo.com.hk/wp-content/uploads/2024/05/digial.png")  # Digital Zoo wordmark,可換
ICON_URL = os.environ.get("ICON_URL", "https://digitalzoo.com.hk/wp-content/uploads/2024/01/cropped-dz-icon-270x270.png")  # DZ 方形徽章,可換/留空
TEST_SEND = os.environ.get("TEST_SEND", "").lower() in ("1", "true", "yes")
JINA_API_KEY = os.environ.get("JINA_API_KEY", "")  # 可選:Jina Reader key(提高全文抓取額度)

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



BROWSER_UA = ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
              "(KHTML, like Gecko) Chrome/124.0.0.0 Safari/537.36")


# 公開免費代理:當網站用 Cloudflare 封 data center IP(例如 Search Engine Land),
# 改由代理用佢自己嘅 IP 代讀,回傳原始 feed bytes。可加多幾個增加成功率。
PROXY_TEMPLATES = [u.strip() for u in os.environ.get(
    "FEED_PROXIES",
    "https://api.allorigins.win/raw?url={url},https://corsproxy.io/?url={url}",
).split(",") if u.strip()]


def _fetch_bytes(url, timeout=45):
    import urllib.request
    req = urllib.request.Request(url, headers={
        "User-Agent": BROWSER_UA,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    })
    with urllib.request.urlopen(req, timeout=timeout) as r:
        return r.read()


def fetch_feed(url):
    """多重後備讀取:直接 → urllib → 公開代理。專治 Cloudflare 403 封鎖。"""
    # 1) 直接帶瀏覽器 UA
    parsed = feedparser.parse(url, request_headers={
        "User-Agent": BROWSER_UA,
        "Accept": "application/rss+xml, application/xml, text/xml, */*",
    })
    if parsed.entries:
        return parsed

    # 2) 用 urllib 自行抓
    try:
        p = feedparser.parse(_fetch_bytes(url))
        if p.entries:
            return p
    except Exception as e:
        print(f"    (直接抓取失敗:{e})")

    # 3) 經公開代理代讀
    import urllib.parse
    enc = urllib.parse.quote(url, safe="")
    for tmpl in PROXY_TEMPLATES:
        proxy_url = tmpl.format(url=enc)
        host = urllib.parse.urlparse(proxy_url).netloc
        try:
            p = feedparser.parse(_fetch_bytes(proxy_url))
            if p.entries:
                print(f"    (經代理成功:{host} → {len(p.entries)} 篇)")
                return p
            print(f"    (代理 {host} 回傳冇文章)")
        except Exception as e:
            print(f"    (代理 {host} 失敗:{e})")

    return parsed


def gather_entries() -> list:
    """讀齊所有來源 feed,合併、按發佈時間新到舊排序,並移除重複連結。"""
    merged = []
    seen_links = set()
    for url in SOURCE_FEEDS:
        parsed = fetch_feed(url)
        if parsed.bozo and not parsed.entries:
            reason = getattr(parsed, "bozo_exception", "")
            status = getattr(parsed, "status", "")
            print(f"  ⚠ 讀唔到來源,跳過:{url}  (status={status} {reason})")
            continue
        print(f"  ✓ {url} → {len(parsed.entries)} 篇")
        for e in parsed.entries:
            link = e.get("link")
            if not link or link in seen_links:
                continue
            if ALLOWED_DOMAINS and not any(d in link.lower() for d in ALLOWED_DOMAINS):
                print(f"    (略過非白名單連結:{link})")
                continue
            seen_links.add(link)
            ts = e.get("published_parsed") or e.get("updated_parsed")
            e["_sort_ts"] = tuple(ts) if ts else (0,)
            merged.append(e)
    merged.sort(key=lambda e: e.get("_sort_ts", (0,)), reverse=True)
    print(f"合共讀到 {len(merged)} 篇文章,來自 {len(SOURCE_FEEDS)} 個來源。")
    return merged


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


def fetch_fulltext_via_jina(url: str) -> str:
    """經 Jina Reader 用真實瀏覽器攞全文,可繞過 Cloudflare。回傳乾淨文字。"""
    import urllib.request
    headers = {"User-Agent": BROWSER_UA, "X-Return-Format": "text"}
    if JINA_API_KEY:
        headers["Authorization"] = "Bearer " + JINA_API_KEY
    req = urllib.request.Request("https://r.jina.ai/" + url, headers=headers)
    with urllib.request.urlopen(req, timeout=60) as r:
        return r.read().decode("utf-8", "ignore")


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

    link = entry.get("link")
    # 全文太短就抓原文:先試直接(SEJ 等通),再試 Jina Reader(過 Cloudflare,SE Land 靠佢)
    if (not text or len(text) < 600) and link:
        try:
            downloaded = trafilatura.fetch_url(link)
            if downloaded:
                full = trafilatura.extract(downloaded)
                if full and len(full) > len(text or ""):
                    text = full
        except Exception:
            pass
    if (not text or len(text) < 600) and link:
        try:
            jt = fetch_fulltext_via_jina(link)
            if jt and len(jt) > len(text or ""):
                text = jt
                print(f"    (Jina 全文成功:{len(jt)} 字)")
        except Exception as e:
            print(f"    (Jina 全文抓取失敗,改用摘要:{e})")

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


def source_name(link: str) -> str:
    l = (link or "").lower()
    if "searchenginejournal" in l:
        return "Search Engine Journal"
    if "searchengineland" in l:
        return "Search Engine Land"
    m = re.search(r"https?://([^/]+)", l)
    return m.group(1).replace("www.", "") if m else "其他來源"


def build_email_html(items: list) -> str:
    today = dt.datetime.now(HKT).strftime("%Y年%m月%d日")
    ACCENT = BRAND_COLOR
    INK = "#1a1a2e"
    MUTED = "#6b7280"
    BORDER = "#ececf1"
    PAGE_BG = "#f0f1f4"

    groups = {}
    for it in items:
        groups.setdefault(source_name(it["link"]), []).append(it)

    icon_img = (f'<img src="{html.escape(ICON_URL)}" alt="" width="38" height="38" style="display:block;width:38px;height:38px;border-radius:9px;border:0;">'
                if ICON_URL else "")
    if LOGO_URL:
        logo_img = f'<img src="{html.escape(LOGO_URL)}" alt="{html.escape(BRAND_NAME)}" height="30" style="display:block;height:30px;width:auto;border:0;">'
    else:
        logo_img = f'<span style="font-size:21px;font-weight:800;color:{INK};">{html.escape(BRAND_NAME)}</span>'
    if icon_img:
        brand = (f'<table role="presentation" cellpadding="0" cellspacing="0"><tr>'
                 f'<td style="padding-right:11px;vertical-align:middle;">{icon_img}</td>'
                 f'<td style="vertical-align:middle;">{logo_img}</td>'
                 f'</tr></table>')
    else:
        brand = logo_img

    sections = []
    for src_name, arr in groups.items():
        cards = []
        for it in arr:
            cards.append(
                f'<tr><td style="padding:20px 0;border-bottom:1px solid {BORDER};">'
                f'<a href="{html.escape(it["link"])}" style="font-size:16px;font-weight:700;color:{INK};text-decoration:none;line-height:1.45;">{html.escape(it["title"])}</a>'
                f'<div style="margin-top:11px;font-size:13.5px;color:#3a3a45;line-height:1.7;">{it["summary_html"]}</div>'
                f'<a href="{html.escape(it["link"])}" style="display:inline-block;margin-top:12px;font-size:12.5px;font-weight:600;color:{ACCENT};text-decoration:none;">閱讀原文 &rarr;</a>'
                f'</td></tr>'
            )
        sections.append(
            f'<tr><td style="padding:30px 34px 4px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0"><tr>'
            f'<td width="4" style="background:{ACCENT};border-radius:2px;line-height:1px;font-size:1px;">&nbsp;</td>'
            f'<td style="padding-left:10px;">'
            f'<span style="font-size:15px;font-weight:800;color:{INK};letter-spacing:.2px;">{html.escape(src_name)}</span>'
            f'<span style="font-size:12px;color:{MUTED};font-weight:600;">　{len(arr)} 篇</span>'
            f'</td></tr></table></td></tr>'
            f'<tr><td style="padding:0 34px;">'
            f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0">{"".join(cards)}</table>'
            f'</td></tr>'
        )

    header = (
        f'<tr><td style="padding:26px 34px 20px;border-bottom:3px solid {ACCENT};">'
        f'{brand}'
        f'<div style="margin-top:14px;font-size:12.5px;color:{MUTED};">每日精選 SEO 行業新文 · AI 中英對照摘要 · {today}</div>'
        f'</td></tr>'
    )
    footer = (
        f'<tr><td style="padding:22px 34px;background:#fafafb;border-top:1px solid {BORDER};font-size:11.5px;color:#9aa0aa;line-height:1.7;">'
        f'內容由 Search Engine Journal 及 Search Engine Land 之文章經 AI 整理重點,版權歸原作者所有,詳情請按標題閱讀原文。<br>'
        f'本摘要為 {html.escape(BRAND_NAME)} 團隊內部 SEO 參考之用。</td></tr>'
    )

    return (
        '<!DOCTYPE html><html><head><meta charset="utf-8">'
        '<meta name="viewport" content="width=device-width,initial-scale=1"></head>'
        f'<body style="margin:0;padding:0;background:{PAGE_BG};">'
        f'<table role="presentation" width="100%" cellpadding="0" cellspacing="0" style="background:{PAGE_BG};padding:28px 12px;">'
        '<tr><td align="center">'
        '<table role="presentation" width="600" cellpadding="0" cellspacing="0" '
        f'style="max-width:600px;width:100%;background:#ffffff;border:1px solid {BORDER};border-radius:14px;overflow:hidden;'
        'font-family:-apple-system,BlinkMacSystemFont,\'Segoe UI\',Roboto,\'Helvetica Neue\',Arial,\'PingFang HK\',\'Microsoft JhengHei\',sans-serif;">'
        f'{header}{"".join(sections)}{footer}'
        '</table>'
        f'<div style="margin-top:14px;font-size:11px;color:#b0b4bd;font-family:Arial,sans-serif;">Powered by {html.escape(BRAND_NAME)} · SEO Intelligence</div>'
        '</td></tr></table></body></html>'
    )


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
    entries = gather_entries()

    # ----- 測試模式:每個來源各抽 2 篇,只寄俾自己,唔改去重記錄 -----
    if TEST_SEND:
        per_source = {}
        sample = []
        for e in entries:
            src = source_name(e.get("link", ""))
            if per_source.get(src, 0) >= 2:   # 每個來源最多 2 篇
                continue
            per_source[src] = per_source.get(src, 0) + 1
            sample.append(e)
        items = []
        for e in sample:
            print(f"[測試] 加工:{e.get('title')}")
            items.append({"title": e.get("title", "(無標題)"), "link": e.get("link", ""),
                          "summary_html": summarise(e.get("title", ""), e.get("link", ""), extract_text(e))})
        today = dt.datetime.now(HKT).strftime("%Y-%m-%d")
        send_email(f"[測試] {BRAND_NAME}|SEO 每日重點 {today}", build_email_html(items), [GMAIL_ADDRESS])
        print(f"[測試] 已寄 {len(items)} 篇樣本俾 {GMAIL_ADDRESS}(來源:{per_source})")
        return

    # ----- 正式模式:只處理未發過嘅新文 -----
    seen = load_seen()
    seen_set = set(seen)
    new_items = []
    for entry in entries:
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
