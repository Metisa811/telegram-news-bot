import json
import os
import re
import sys
import time
import feedparser
import requests
from google import genai

# ================= تنظیمات و کلیدها =================
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GEMINI_API_KEY = os.environ.get("GEMINI_API_KEY")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not GEMINI_API_KEY:
    print("❌ خطا: متغیرهای Secret تنظیم نشده‌اند.")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)
AVAILABLE_MODELS = ["gemini-3.8-flash"]
DB_FILE = "sent_articles.json"

# لیست کانال‌های تلگرامی برای رصد خبرهای فوری
TELEGRAM_CHANNELS = [
    "ProjectLeon7",
    "idfinfarsi",
    "withyashar"
]

# فیدهای RSS برای اخبار جنگ، منطقه و سیاست اروپا
RSS_FEEDS = [
    "https://news.google.com/rss/search?q=Iran+OR+Israel+OR+Hezbollah+OR+%22Middle+East%22+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=European+Union+OR+NATO+OR+%22Europe+politics%22+when:1d&hl=en-US&gl=US&ceid=US:en",
    "https://news.google.com/rss/search?q=Iran+politics+OR+Tehran+when:1d&hl=en-US&gl=US&ceid=US:en"
]

# ================= ذخیره و خواندن تاریخچه =================
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

# ================= دریافت پست‌های کانال تلگرام =================
def fetch_telegram_channel_posts(username):
    url = f"https://t.me/s/{username}"
    headers = {
        "User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 (KHTML, like Gecko) Chrome/120.0.0.0 Safari/537.36"
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
                    
                    # نادیده گرفتن پیام‌های تک‌کلمه‌ای یا بسیار کوتاه
                    if len(clean_text) > 40:
                        first_line = clean_text.split('\n')[0][:70]
                        posts.append({
                            "id": f"tg_{post_id}",
                            "title": first_line,
                            "summary": clean_text,
                            "link": f"https://t.me/{post_id}"
                        })
    except Exception as e:
        print(f"⚠️ خطا در دریافت کانال @{username}: {e}")
    return posts

# ================= پردازش خبر با جمنای =================
def summarize_with_gemini(title, summary, link):
    prompt = f"""
نقش تو: ادیتور ارشد و تحلیل‌گر ارشد میز خبر فوری و اتاق وضعیت مسائل خاورمیانه، ایران و اروپا.

متن ورودی (ممکن است فارسی یا انگلیسی باشد):
عنوان/تیتر: {title}
متن خبر: {summary}

دستورالعمل بازنویسی:
این خبر را به فارسی فاخر، سلیس، بی‌طرف و با قالب اختصاصی فوری برای کانال تلگرام بازنویسی کن:

🚨 [تیتر جذاب و فوری همراه با ایموجی مرتبط]

🔹 جزئیات میدان و تحولات:
- رویداد اصلی و طرف‌های ماجرا دقیقاً چه می‌گویند؟
- مواضع میدانی یا سیاسی طرفین (ایران، آمریکا، اسرائیل یا اروپا)
- فکت‌های تایید شده، شواهد یا پیامدهای مستقیم میدانی

⚠️ ارزیابی و پیامد:
یک پاراگراف کوتاه (حداکثر ۲ جمله) درباره اینکه این رخداد چه اثری بر توازن قدرت، خطر درگیری نظامی یا وضعیت اقتصادی می‌گذارد.

نکات الزامی:
- اگر خبر حاوی شایعه یا ادعای یک‌طرفه است، حتماً با قید «به ادعای...» یا «گزارش‌های تایید نشده» ذکر کن.
- هیچ پیش‌گفتار، سلام، توضیح یا متن اضافی ننویس.
"""
    for model_name in AVAILABLE_MODELS:
        for attempt in range(1, 4):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
                text = response.text.strip()
                return f"{text}\n\n🔗 [مشاهده منبع خبر]({link})"
            except Exception as e:
                err_str = str(e)
                if "503" in err_str:
                    time.sleep(6)
                else:
                    print(f"⚠️ خطای جمنای ({model_name}): {err_str}")
                    break
    return None

# ================= ارسال پیام به تلگرام =================
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
            # ارسال ساده در صورت وجود ایراد ساختاری در کاراکترهای مارک‌داون
            payload.pop("parse_mode", None)
            res = requests.post(url, json=payload, timeout=15)
            res_json = res.json()
            if not res_json.get("ok"):
                print(f"❌ خطای تلگرام: {res_json}")
                return False
        print("✅ خبر در کانال منتشر شد.")
        return True
    except Exception as e:
        print(f"❌ خطای اتصال به تلگرام: {e}")
        return False

# ================= تابع اصلی اجرا =================
def main():
    sent_articles = load_sent_articles()
    all_candidates = []

    # ۱. استخراج خبرها از کانال‌های تلگرامی هدف
    print("📡 در حال دریافت خبرها از کانال‌های تلگرام...")
    for ch in TELEGRAM_CHANNELS:
        ch_posts = fetch_telegram_channel_posts(ch)
        all_candidates.extend(ch_posts[-4:])  # ۴ پیام آخر هر کانال

    # ۲. استخراج خبرها از فیدهای RSS جهانی و منطقه‌ای (۱۰ خبر آخر هر فید)
    print("📰 در حال بررسی فیدهای خبری...")
    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        for entry in feed.entries[:8]:
            article_id = entry.get("id", entry.get("link"))
            all_candidates.append({
                "id": article_id,
                "title": entry.get("title", ""),
                "summary": entry.get("summary", ""),
                "link": entry.get("link", "")
            })

    # حداکثر ۳ خبر جدید در هر نوبت ارسال می‌شود
    sent_count = 0
    max_news_per_run = 3
    new_sent = False

    for item in all_candidates:
        if sent_count >= max_news_per_run:
            break

        if item["id"] not in sent_articles:
            print(f"📌 پردازش رویداد جدید: {item['title']}")
            summary = summarize_with_gemini(
                item["title"],
                item["summary"],
                item["link"]
            )
            if summary and send_to_telegram(summary):
                sent_articles.add(item["id"])
                new_sent = True
                sent_count += 1
                save_sent_articles(sent_articles)

                if sent_count < max_news_per_run:
                    print("⏳ وقفه ۳ دقیقه‌ای تا ارسال خبر بعدی...")
                    time.sleep(180)
        else:
            continue

    if new_sent:
        print(f"✅ تعداد {sent_count} خبر جدید با وقفه ارسال شد.")
    else:
        print("مورد جدیدی یافت نشد.")

if __name__ == "__main__":
    main()
