import json
import os
import re
import sys
import time
import feedparser
import requests
from google import genai

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not GEMINI_API_KEY:
    print("❌ خطا: متغیرهای Secret تنظیم نشده‌اند.")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)
AVAILABLE_MODELS = ["gemini-3.8-flash"]
DB_FILE = "sent_articles.json"

TELEGRAM_CHANNELS = [
    "ProjectLeon7",
    "idfinfarsi",
    "withyashar"
]

RSS_FEEDS = [
    "https://news.google.com/rss/search?q=Iran+OR+Israel+OR+Hezbollah+OR+%22Middle+East%22+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=Iran+politics+OR+Tehran+when:1d&hl=en-US&gl=US&ceid=US:en"
]

def load_sent_articles():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception:
            return set()
    return set()

def save_sent_articles(articles):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(list(articles), f, ensure_ascii=False, indent=2)

def fetch_telegram_channel_posts(username):
    url = f"https://t.me/s/{username}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"
    }
    posts = []
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            parts = response.text.split('data-post="')
            for part in parts[1:]:
                post_id = part.split('"', 1)[0]
                text_match = re.search(r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>', part, re.DOTALL)
                if text_match:
                    raw_html = text_match.group(1)
                    raw_html = re.sub(r'<br\s*/?>', '\n', raw_html)
                    clean_text = re.sub(r'<[^>]+>', '', raw_html).strip()
                    if len(clean_text) > 40:
                        first_line = clean_text.split('\n')[0][:70]
                        posts.append({
                            "id": f"tg_{post_id}",
                            "title": first_line,
                            "summary": clean_text
                        })
    except Exception as e:
        print(f"⚠️ خطا در کانال @{username}: {e}")
    return posts

def summarize_with_gemini(title, summary):
    prompt = f"""
نقش: ادیتور خبر فوری (Breaking News) در تلگرام.

متن خبر:
تیتر: {title}
متن: {summary}

دستورالعمل تولید متن:
خبر را بسیار کوتاه، گزیده، بدون اطناب و به سبک فوری بنویس:
- خط اول: 🚨 [یک تیتر کوتاه و جذاب همراه با ایموجی مناسب]
- خطوط بعدی: نهایتاً ۲ الی ۳ بولت بسیار کوتاه (هر بولت نهایتاً یک سطر) که فقط اصل فکت و اصل اتفاق را بگوید.
- خط آخر: یک جمله فوق‌العاده کوتاه درباره پیامد یا اهمیت خبر.

قوانین مهم:
- اصلاً لینک یا متنی تحت عنوان «منبع» نگذار.
- زیاده‌گویی نکن و حجم کل متن زیر ۵۰ کلمه باشد.
- هیچ پیش‌گفتار، سلام یا توضیحی خارج از قالب ننویس.
"""
    try:
        response = client.models.generate_content(
            model="gemini-3.8-flash",
            contents=prompt,
        )
        return response.text.strip()
    except Exception as e:
        err = str(e)
        print(f"⚠️ خطای جمنای: {err}")
        if "429" in err or "RESOURCE_EXHAUSTED" in err:
            return "QUOTA_EXCEEDED"
        return None

def send_to_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True
    }
    try:
        res = requests.post(url, json=payload, timeout=15)
        res_json = res.json()
        if not res_json.get("ok"):
            payload.pop("parse_mode", None)
            res = requests.post(url, json=payload, timeout=15)
            res_json = res.json()
            if not res_json.get("ok"):
                return False
        return True
    except Exception:
        return False

def main():
    sent_articles = load_sent_articles()
    candidates = []

    # استخراج پست‌های تلگرام
    for ch in TELEGRAM_CHANNELS:
        posts = fetch_telegram_channel_posts(ch)
        candidates.extend(posts[-2:])

    # استخراج فیدهای RSS
    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries[:2]:
            candidates.append({
                "id": entry.get("id", entry.get("link")),
                "title": entry.get("title", ""),
                "summary": entry.get("summary", "")
            })

    unprocessed = [c for c in candidates if c["id"] not in sent_articles]
    print(f"تعداد خبرهای جدید واجد شرایط: {len(unprocessed)}")

    max_send = 2
    sent_count = 0

    for item in unprocessed:
        if sent_count >= max_send:
            break

        print(f"📌 پردازش: {item['title'][:40]}...")
        summary = summarize_with_gemini(item["title"], item["summary"])

        if summary == "QUOTA_EXCEEDED":
            print("⛔ سهمیه تمام است.")
            break

        if summary and send_to_telegram(summary):
            print("✅ خبر در تلگرام پست شد.")
            sent_articles.add(item["id"])
            save_sent_articles(sent_articles)
            sent_count += 1

            if sent_count < max_send:
                print("⏳ وقفه ۶۰ ثانیه‌ای...")
                time.sleep(60)

    print("پایان عملیات.")

if __name__ == "__main__":
    main()
