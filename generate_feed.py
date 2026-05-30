#!/usr/bin/env python3
"""
SEJ SEO 專欄 → AI 加工 RSS feed 生成器(OpenRouter 版)
------------------------------------------------
讀取 Search Engine Journal 嘅 SEO 專欄 RSS,
經 OpenRouter 用平價模型為每篇「新」文章生成中英對照重點
(各 3-4 點,中文約 200-300 字),再輸出一條新嘅 RSS feed
俾 Brevo 嘅 RSS Campaign 訂閱。

本地測試(進階用家先需要):
    OPENROUTER_API_KEY=sk-or-... python generate_feed.py
    然後開 docs/feed.xml 睇結果。
新手可以完全唔掂呢個檔案,跟 README 喺 GitHub 網頁度貼上去就得。
"""

import os
import re
import json
import html
import datetime as dt
from email.utils import format_datetime
from pathlib import Path

import feedparser
import trafilatura
from openai import OpenAI

# ---------- 設定(可用環境變數覆寫)----------
SOURCE_FEED = os.environ.get(
    "SOURCE_FEED",
    "https://www.searchenginejournal.com/category/seo/feed/",
)
# 去 https://openrouter.ai/models 撳「Price: Low to High」,copy 你想用嘅 model ID 貼喺度。
# 例:google/gemini-2.0-flash-001(平靚正)、deepseek/deepseek-chat(更平)、
#     或者免費試:加 :free 尾(有速率限制)。
MODEL = os.environ.get("MODEL", "deepseek-v4-flash")
MAX_ITEMS = int(os.environ.get("MAX_ITEMS", "20"))       # feed 內保留最近幾多篇
OUTPUT_DIR = Path(os.environ.get("OUTPUT_DIR", "docs"))  # GitHub Pages 由 /docs 派發
FEED_PATH = OUTPUT_DIR / "feed.xml"
STATE_PATH = OUTPUT_DIR / "state.json"

FEED_TITLE = "SEJ SEO 每日重點(AI 中英對照)"
FEED_LINK = os.environ.get("PUBLIC_FEED_URL", "https://ansoncky64-1996.github.io/SEJ-Digest/feed.xml")
FEED_DESC = "Search Engine Journal SEO 專欄最新文章,經 AI 整理成中英對照重點。"

# OpenRouter 係 OpenAI 相容,只係改 base_url + key
client = OpenAI(
    base_url="https://openrouter.ai/api/v1",
    api_key=os.environ.get("OPENROUTER_API_KEY"),
)

PROMPT = """你係一個為香港 SEO 團隊服務嘅新聞編輯。以下係一篇 Search Engine Journal 嘅 SEO 文章。

請將佢濃縮成「中英對照重點」,規格如下:
- 先英文版,後繁體中文版,兩個版本內容互相對應
- 每個版本 3 至 4 個 bullet point
- 繁體中文部分總字數約 200–300 字,用香港讀者睇得明嘅書面語
- 聚焦「對 SEO 工作有咩實際啟示 / action」,唔好淨係複述標題
- 唔好捏造文中冇出現過嘅數據或結論

只輸出以下 HTML 片段(俾 email 用),唔好有任何其他文字、唔好用 markdown code fence:

<p><strong>🔑 Key Takeaways</strong></p>
<ul>
<li>...</li>
<li>...</li>
</ul>
<p><strong>🔑 重點(繁體中文)</strong></p>
<ul>
<li>...</li>
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


def load_state() -> dict:
    if STATE_PATH.exists():
        return json.loads(STATE_PATH.read_text(encoding="utf-8"))
    return {"items": {}}  # {link: {title, link, pub_iso, summary_html}}


def save_state(state: dict) -> None:
    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    STATE_PATH.write_text(json.dumps(state, ensure_ascii=False, indent=2), encoding="utf-8")


def extract_text(entry) -> str:
    """攞文章全文純文字,優先用 feed 內嘅 content,唔夠就 fetch 原文。"""
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

    # feed 內容太短(SEJ 有時只俾摘要)就抓原文
    if (not text or len(text) < 400) and entry.get("link"):
        try:
            downloaded = trafilatura.fetch_url(entry["link"])
            if downloaded:
                full = trafilatura.extract(downloaded)
                if full and len(full) > len(text or ""):
                    text = full
        except Exception:
            pass

    return (text or entry.get("title", ""))[:8000]  # 控制 token 用量


def summarise(title: str, link: str, body: str) -> str:
    resp = client.chat.completions.create(
        model=MODEL,
        max_tokens=1200,
        messages=[{"role": "user", "content": PROMPT.format(title=title, link=link, body=body)}],
    )
    out = (resp.choices[0].message.content or "").strip()
    # 保險:去走可能出現嘅 code fence
    if out.startswith("```"):
        out = out.strip("`")
        out = out.split("\n", 1)[1] if "\n" in out else out
    # 防止 CDATA 被 "]]>" 截斷
    return out.replace("]]>", "]]&gt;")


def pub_iso_of(entry) -> str:
    if entry.get("published_parsed"):
        return dt.datetime(*entry.published_parsed[:6], tzinfo=dt.timezone.utc).isoformat()
    return dt.datetime.now(dt.timezone.utc).isoformat()


def build_rss(items: list) -> str:
    now = format_datetime(dt.datetime.now(dt.timezone.utc))
    parts = [
        '<?xml version="1.0" encoding="UTF-8"?>',
        '<rss version="2.0" xmlns:content="http://purl.org/rss/1.0/modules/content/" '
        'xmlns:atom="http://www.w3.org/2005/Atom">',
        "<channel>",
        f"<title>{html.escape(FEED_TITLE)}</title>",
        f"<link>{html.escape(FEED_LINK)}</link>",
        f"<description>{html.escape(FEED_DESC)}</description>",
        "<language>zh-hk</language>",
        f"<lastBuildDate>{now}</lastBuildDate>",
        f'<atom:link href="{html.escape(FEED_LINK)}" rel="self" type="application/rss+xml"/>',
    ]
    for it in items:
        pub = dt.datetime.fromisoformat(it["pub_iso"])
        if pub.tzinfo is None:
            pub = pub.replace(tzinfo=dt.timezone.utc)
        parts += [
            "<item>",
            f"<title>{html.escape(it['title'])}</title>",
            f"<link>{html.escape(it['link'])}</link>",
            f'<guid isPermaLink="true">{html.escape(it["link"])}</guid>',
            f"<pubDate>{format_datetime(pub)}</pubDate>",
            f"<description><![CDATA[{it['summary_html']}]]></description>",
            f"<content:encoded><![CDATA[{it['summary_html']}]]></content:encoded>",
            "</item>",
        ]
    parts += ["</channel>", "</rss>"]
    return "\n".join(parts)


def main():
    state = load_state()
    parsed = feedparser.parse(SOURCE_FEED)
    if parsed.bozo and not parsed.entries:
        raise SystemExit(f"無法讀取來源 feed: {SOURCE_FEED}")

    new_count = 0
    for entry in parsed.entries:
        link = entry.get("link")
        if not link or link in state["items"]:
            continue  # 已處理過 → 唔會重複 summarise(慳錢兼去重)
        title = entry.get("title", "(無標題)")
        print(f"處理新文章:{title}")
        try:
            summary = summarise(title, link, extract_text(entry))
        except Exception as e:
            print(f"  總結失敗,今次跳過:{e}")
            continue
        state["items"][link] = {
            "title": title,
            "link": link,
            "pub_iso": pub_iso_of(entry),
            "summary_html": summary,
        }
        new_count += 1

    # 按發佈時間排序,只保留最近 MAX_ITEMS 篇
    kept = sorted(state["items"].values(), key=lambda x: x["pub_iso"], reverse=True)[:MAX_ITEMS]
    state["items"] = {it["link"]: it for it in kept}

    OUTPUT_DIR.mkdir(parents=True, exist_ok=True)
    FEED_PATH.write_text(build_rss(kept), encoding="utf-8")
    save_state(state)
    print(f"完成:今次新增 {new_count} 篇,feed 現有 {len(kept)} 篇 → {FEED_PATH}")


if __name__ == "__main__":
    main()
