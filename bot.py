import json
import os
import feedparser
import requests
from google import genai

# خواندن اطلاعات از متغیرهای گیت‌هاب
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

RSS_URL = "https://news.google.com/rss?hl=en-US&gl=US&ceid=US:en"
DB_FILE = "sent_articles.json"

client = genai.Client(api_key=GEMINI_API_KEY)

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
    try:
        response = client.models.generate_content(
            model="gemini-2.5-flash",
            contents=prompt,
        )
        text = response.text.strip()
        return f"{text}\n\n🔗 [منبع اصلی]({link})"
    except Exception as e:
        print(f"Error calling Gemini: {e}")
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
        return res.status_code == 200
    except Exception as e:
        print(f"Error sending to Telegram: {e}")
        return False

def main():
    sent_articles = load_sent_articles()
    feed = feedparser.parse(RSS_URL)
    new_sent = False

    # بررسی ۳ خبر تازه
    for entry in reversed(feed.entries[:5]):
        article_id = entry.get("id", entry.get("link"))
        if article_id not in sent_articles:
            print(f"خبر تازه: {entry.get('title')}")
            summary = summarize_with_gemini(
                entry.get("title", ""),
                entry.get("summary", ""),
                entry.get("link", "")
            )
            if summary and send_to_telegram(summary):
                sent_articles.add(article_id)
                new_sent = True

    if new_sent:
        save_sent_articles(sent_articles)

if __name__ == "__main__":
    main()
