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

# مدل‌های فعال و پیشنهادی رسمی گوگل
AVAILABLE_MODELS = [
    "gemini-3.1-pro-preview",
    "gemini-3.8-flash"
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
تو یک نویسنده و ادیتور خبر حرفه‌ای هستی. این خبر انگلیسی را به فارسی روان و جذاب خلاصه کن.

عنوان خبر: {title}
متن خام خبر: {summary}

قالب خروجی:
- تیتر جذاب در خط اول همراه با ایموجی مرتبط
- ۳ بولت‌پوینت خوانا و کلیدی از متن
- یک خط جمع‌بندی درباره اهمیت رویداد
متن آماده تلگرام باشد و هیچ توضیح یا پیش‌گفتاری اضافه نکن.
"""
    for model_name in AVAILABLE_MODELS:
        # ۳ بار تلاش برای هر مدل در صورت شلوغی سرور (خطای 503)
        for attempt in range(1, 4):
            try:
                print(f"ارسال به {model_name} (تلاش {attempt})...")
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
                text = response.text.strip()
                print(f"✅ خلاصه با موفقیت توسط {model_name} تولید شد.")
                return f"{text}\n\n🔗 [منبع اصلی خبر]({link})"
            except Exception as e:
                err_str = str(e)
                if "503" in err_str:
                    print(f"⏳ ترافیک لحظه‌ای سرور روی {model_name}. ۵ ثانیه انتظار...")
                    time.sleep(5)
                else:
                    print(f"⚠️ خطای مدل {model_name}: {err_str}")
                    break

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
            # اگر خطای پارس مارک‌داون داد، پیام به صورت متن ساده ارسال می‌شود
            payload.pop("parse_mode", None)
            res = requests.post(url, json=payload, timeout=15)
            res_json = res.json()
            if not res_json.get("ok"):
                print(f"❌ خطای تلگرام: {res_json}")
                return False
        print("✅ پیام با موفقیت به کانال تلگرام ارسال شد.")
        return True
    except Exception as e:
        print(f"❌ خطای اتصال به تلگرام: {e}")
        return False

def main():
    sent_articles = load_sent_articles()
    print(f"تعداد خبرهای ذخیره شده قبلی: {len(sent_articles)}")
    
    feed = feedparser.parse(RSS_URL)
    print(f"تعداد خبرهای فید: {len(feed.entries)}")
    new_sent = False

    for entry in reversed(feed.entries[:3]):
        article_id = entry.get("id", entry.get("link"))
        if article_id not in sent_articles:
            print(f"درحال پردازش: {entry.get('title')}")
            summary = summarize_with_gemini(
                entry.get("title", ""),
                entry.get("summary", ""),
                entry.get("link", "")
            )
            if summary and send_to_telegram(summary):
                sent_articles.add(article_id)
                new_sent = True
                time.sleep(3)
        else:
            print("خبر قبلاً ارسال شده است.")

    if new_sent:
        save_sent_articles(sent_articles)
        print("لیست خبرها به‌روزرسانی شد.")

if __name__ == "__main__":
    main()
