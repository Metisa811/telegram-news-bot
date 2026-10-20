import json
import os
import sys
import feedparser
import requests
from google import genai

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

RSS_URL = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
DB_FILE = "sent_articles.json"

# بررسی اولیه متغیرها
if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not GEMINI_API_KEY:
    print("❌ خطا: یکی از متغیرهای Secret تعریف نشده است!")
    print(f"BOT_TOKEN: {'OK' if TELEGRAM_BOT_TOKEN else 'MISSING'}")
    print(f"CHAT_ID: {'OK' if TELEGRAM_CHAT_ID else 'MISSING'}")
    print(f"GEMINI_KEY: {'OK' if GEMINI_API_KEY else 'MISSING'}")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)

def load_sent_articles():
    if os.path.exists(DB_FILE):
        try:
            with open(DB_FILE, "r", encoding="utf-8") as f:
                return set(json.load(f))
        except Exception as e:
            print(f"هشدار در خواندن فایل تاریخچه: {e}")
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
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        text = response.text.strip()
        return f"{text}\n\n🔗 [منبع اصلی]({link})"
    except Exception as e:
        print(f"❌ خطا در جمنای: {e}")
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
            print(f"❌ پاسخ خطای تلگرام: {res_json}")
            return False
        print("✅ پیام با موفقیت به تلگرام ارسال شد.")
        return True
    except Exception as e:
        print(f"❌ خطا در اتصال به تلگرام: {e}")
        return False

def main():
    sent_articles = load_sent_articles()
    print(f"تعداد خبرهای ارسال‌شده قبلی: {len(sent_articles)}")
    
    feed = feedparser.parse(RSS_URL)
    print(f"تعداد خبرهای دریافت شده از فید: {len(feed.entries)}")
    
    new_sent = False

    # بررسی ۳ خبر اول
    for entry in reversed(feed.entries[:3]):
        article_id = entry.get("id", entry.get("link"))
        if article_id not in sent_articles:
            print(f"درحال پردازش خبر: {entry.get('title')}")
            summary = summarize_with_gemini(
                entry.get("title", ""),
                entry.get("summary", ""),
                entry.get("link", "")
            )
            if summary:
                if send_to_telegram(summary):
                    sent_articles.add(article_id)
                    new_sent = True
            else:
                print("خلاصه جمنای خالی بود.")
        else:
            print("خبر قبلاً ارسال شده است.")

    if new_sent:
        save_sent_articles(sent_articles)
        print("لیست خبرها به‌روزرسانی شد.")

if __name__ == "__main__":
    main()
