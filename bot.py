import json
import os
import sys
import time
import feedparser
import requests
from google import genai

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

RSS_URL = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
DB_FILE = "sent_articles.json"

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not GEMINI_API_KEY:
    print("❌ خطا: متغیرهای Secret تنظیم نشده‌اند.")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

# لیست مدل‌ها به ترتیب اولویت (در صورت شلوغی، بعدی تست می‌شود)
AVAILABLE_MODELS = [
    "gemini-3.8-flash",
    "gemini-3.6-flash",
    "gemini-2.5-pro",
    "gemini-2.0-flash",
    "gemini-1.5-flash"
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

def summarize_with_gemini(title, summary, link):
    prompt = f"""
تو یک نویسنده حرفه‌ای کانال خبری تلگرام هستی.
این خبر انگلیسی را بخوان و به فارسی روان و جذاب خلاصه کن:

عنوان: {title}
متن خام: {summary}

قالب خروجی:
- تیتر جذاب با ایموجی مرتبط
- ۳ بولت‌پوینت کوتاه، دقیق و خوانا
- یک نتیجه‌گیری یک‌جمله‌ای درباره چرایی اهمیت خبر
فقط متن آماده ارسال به تلگرام را بدون هیچ مقدمه یا توضیح اضافه‌ای بنویس.
"""
    for model_name in AVAILABLE_MODELS:
        try:
            response = client.models.generate_content(
                model=model_name,
                contents=prompt,
            )
            text = response.text.strip()
            print(f"✅ خلاصه با موفقیت توسط {model_name} تولید شد.")
            return f"{text}\n\n🔗 [منبع اصلی]({link})"
        except Exception as e:
            print(f"⚠️ مدل {model_name} پاسخ نداد ({e}). تلاش با مدل بعدی...")
            time.sleep(2)

    return None

def send_to_telegram(message):
    url = f"https://api.telegram.org/bot{TELEGRAM_BOT_TOKEN}/sendMessage"
    payload = {
        "chat_id": TELEGRAM_CHAT_ID,
        "text": message,
        "parse_mode": "Markdown",
        "disable_web_page_preview": False
    }
    try:
        res = requests.post(url, json=payload, timeout=15)
        res_json = res.json()
        if not res_json.get("ok"):
            print(f"❌ خطای تلگرام: {res_json}")
            return False
        print("✅ پیام با موفقیت به تلگرام ارسال شد.")
        return True
    except Exception as e:
        print(f"❌ خطا در اتصال به تلگرام: {e}")
        return False

def main():
    sent_articles = load_sent_articles()
    feed = feedparser.parse(RSS_URL)
    new_sent = False

    for entry in reversed(feed.entries[:3]):
        article_id = entry.get("id", entry.get("link"))
        if article_id not in sent_articles:
            print(f"درحال پردازش خبر: {entry.get('title')}")
            summary = summarize_with_gemini(
                entry.get("title", ""),
                entry.get("summary", ""),
                entry.get("link", "")
            )
            if summary and send_to_telegram(summary):
                sent_articles.add(article_id)
                new_sent = True
                time.sleep(3)  # وقفه کوتاه بین پیام‌ها

    if new_sent:
        save_sent_articles(sent_articles)
        print("فایل شناسه خبرها به‌روزرسانی شد.")

if __name__ == "__main__":
    main()
