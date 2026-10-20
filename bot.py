import html
import json
import os
import re
import sys
import time

import feedparser
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not GROQ_API_KEY:
    print("❌ خطا: متغیرهای Secret تنظیم نشده‌اند.")
    sys.exit(1)

# اول مدل قوی‌تر، اگر سهمیه‌اش تمام شد مدل سریع‌تر
MODELS = ["llama-3.3-70b-versatile", "llama-3.1-8b-instant"]

DB_FILE = "sent_articles.json"
DB_MAX = 3000            # حداکثر تعداد شناسه‌های ذخیره‌شده
INTERVAL_SECONDS = 120   # فاصله بین هر دور
MAX_SEND_PER_CYCLE = 1   # هر دور حداکثر چند خبر
MAX_TRIES_PER_CYCLE = 3  # هر دور حداکثر چند خبر را به هوش مصنوعی بده
RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "330"))

TELEGRAM_CHANNELS = [
    "ProjectLeon7",
    "idfinfarsi",
    "withyashar",
]

# منابع اولویت‌دار
PRIORITY_FEEDS = [
    "https://news.google.com/rss/search?q=Iran+OR+Israel+OR+Hezbollah+OR+%22Middle+East%22+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=Iran+politics+OR+Tehran+when:1d&hl=en-US&gl=US&ceid=US:en",
]

# منابع عمومی برای وقتی که خبر اولویت‌دار نیست
GENERAL_FEEDS = [
    "http://feeds.bbci.co.uk/news/world/rss.xml",
    "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en",
]

PRIORITY_KEYWORDS = [
    "iran", "ایران", "israel", "اسرائیل", "trump", "ترامپ", "hezbollah",
    "حزب‌الله", "حزب الله", "tehran", "تهران", "idf", "irgc", "سپاه",
    "موشک", "missile", "حمله", "attack", "strike", "gaza", "غزه",
    "lebanon", "لبنان", "nuclear", "هسته", "houthi", "حوثی", "ukraine",
    "اوکراین", "nato", "ناتو", "europe", "اروپا",
]


# ---------- دیتابیس ----------
def load_sent():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                data = json.load(f)
                if isinstance(data, list):
                    return data
        except Exception:
            pass
    return []


def save_sent(sent_list):
    with open(DB_FILE, "w", encoding="utf-8") as f:
        json.dump(sent_list[-DB_MAX:], f, ensure_ascii=False, indent=2)


# ---------- جمع‌آوری خبر ----------
def clean_html(text):
    text = re.sub(r"<br\s*/?>", "\n", text or "")
    text = re.sub(r"<[^>]+>", "", text)
    return html.unescape(text).strip()


def fetch_telegram_channel_posts(username):
    url = f"https://t.me/s/{username}"
    headers = {"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36"}
    posts = []
    try:
        response = requests.get(url, headers=headers, timeout=15)
        if response.status_code == 200:
            parts = response.text.split('data-post="')
            for part in parts[1:]:
                post_id = part.split('"', 1)[0]
                m = re.search(
                    r'<div class="tgme_widget_message_text[^"]*"[^>]*>(.*?)</div>',
                    part, re.DOTALL)
                if m:
                    clean_text = clean_html(m.group(1))
                    if len(clean_text) > 40:
                        posts.append({
                            "id": f"tg_{post_id}",
                            "title": clean_text.split("\n")[0][:70],
                            "summary": clean_text,
                            "priority": True,
                        })
    except Exception as e:
        print(f"⚠️ خطا در کانال @{username}: {e}")
    return posts


def fetch_rss(url, limit, force_priority=False):
    items = []
    try:
        feed = feedparser.parse(url)
        for entry in feed.entries[:limit]:
            items.append({
                "id": entry.get("id", entry.get("link")),
                "title": clean_html(entry.get("title", "")),
                "summary": clean_html(entry.get("summary", "")),
                "priority": force_priority,
            })
    except Exception as e:
        print(f"⚠️ خطا در RSS: {e}")
    return items


def is_priority(item):
    if item.get("priority"):
        return True
    t = (item["title"] + " " + item["summary"]).lower()
    return any(k in t for k in PRIORITY_KEYWORDS)


# ---------- هوش مصنوعی (Groq) ----------
def summarize(title, summary):
    prompt = f"""
نقش: ادیتور خبر فوری (Breaking News) در تلگرام.

متن خبر:
تیتر: {title}
متن: {summary[:1500]}

دستورالعمل تولید متن:
خبر را (حتی اگر انگلیسی است) به فارسی روان، بسیار کوتاه و به سبک فوری بنویس:
- خط اول: 🚨 [یک تیتر کوتاه و جذاب همراه با ایموجی مناسب]
- خطوط بعدی: نهایتاً ۲ الی ۳ بولت بسیار کوتاه (هر بولت یک سطر) که فقط اصل فکت را بگوید.
- خط آخر: یک جمله فوق‌العاده کوتاه درباره پیامد یا اهمیت خبر.

قوانین مهم:
- اصلاً لینک یا متنی تحت عنوان «منبع» نگذار.
- حجم کل متن زیر ۵۰ کلمه باشد.
- هیچ پیش‌گفتار، سلام یا توضیحی خارج از قالب ننویس.
"""
    for model in MODELS:
        try:
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json={
                    "model": model,
                    "temperature": 0.3,
                    "messages": [{"role": "user", "content": prompt}],
                },
                timeout=30,
            )
            if r.status_code == 429:
                print(f"⏳ سهمیه {model} پر است، مدل بعدی...")
                continue
            if r.status_code != 200:
                print(f"⚠️ Groq {model}: {r.status_code} {r.text[:200]}")
                continue
            text = r.json()["choices"][0]["message"]["content"].strip()
            if text:
                return text
        except Exception as e:
            print(f"⚠️ خطای Groq ({model}): {e}")
    return None


# ---------- تلگرام ----------
def send_to_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": True,
    }
    try:
        res = requests.post(url, json=payload, timeout=15).json()
        if not res.get("ok"):
            payload.pop("parse_mode", None)
            res = requests.post(url, json=payload, timeout=15).json()
            if not res.get("ok"):
                print(f"⚠️ تلگرام: {res}")
                return False
        return True
    except Exception as e:
        print(f"⚠️ خطای تلگرام: {e}")
        return False


# ---------- یک دور کار ----------
def run_once():
    sent_list = load_sent()
    sent_set = set(sent_list)
    candidates = []

    for ch in TELEGRAM_CHANNELS:
        candidates.extend(fetch_telegram_channel_posts(ch)[-4:])
    for url in PRIORITY_FEEDS:
        candidates.extend(fetch_rss(url, 6, force_priority=True))
    for url in GENERAL_FEEDS:
        candidates.extend(fetch_rss(url, 8))

    # حذف تکراری‌ها و خبرهای قبلاً ارسال‌شده
    seen = set()
    unprocessed = []
    for c in candidates:
        if not c["id"] or c["id"] in sent_set or c["id"] in seen:
            continue
        seen.add(c["id"])
        unprocessed.append(c)

    # اول خبرهای اولویت‌دار، بعد بقیه (ترتیب داخلی حفظ می‌شود)
    unprocessed.sort(key=lambda c: not is_priority(c))

    print(f"خبر جدید: {len(unprocessed)}")

    sent_count = 0
    tries = 0
    for item in unprocessed:
        if sent_count >= MAX_SEND_PER_CYCLE or tries >= MAX_TRIES_PER_CYCLE:
            break
        tries += 1
        tag = "⭐" if is_priority(item) else "📰"
        print(f"{tag} پردازش: {item['title'][:50]}")

        text = summarize(item["title"], item["summary"])
        if text and send_to_telegram(text):
            print("✅ ارسال شد.")
            sent_list.append(item["id"])
            save_sent(sent_list)
            sent_count += 1


def main():
    deadline = time.time() + RUN_MINUTES * 60
    print(f"شروع؛ مدت اجرا: {RUN_MINUTES:.0f} دقیقه")
    while True:
        try:
            run_once()
        except Exception as e:
            print(f"⚠️ خطای غیرمنتظره: {e}")
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(INTERVAL_SECONDS, remaining))
    print("پایان عملیات.")


if __name__ == "__main__":
    main()
