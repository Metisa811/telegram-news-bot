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

# فیدهای تفکیک‌شده بر اساس اولویت‌های درخواستی شما:
# ۱. تنش ایران، اسرائیل، آمریکا و خاورمیانه
# ۲. اخبار سیاسی اروپا و تحولات بین‌المللی
# ۳. رصد توئیت‌ها و خبرهای فوری به سبک X
RSS_FEEDS = [
    # جستجوی کلیدواژه‌های تنش ایران، اسرائیل و آمریکا در اخبار فوری بین‌المللی
    "https://news.google.com/rss/search?q=Iran+OR+Israel+OR+Hezbollah+OR+%22Middle+East%22+when:1d&hl=en-US&gl=US&ceid=US:en",
    # اخبار سیاسی اروپا و تصمیمات امنیتی/دیپلماتیک
    "https://news.google.com/rss/search?q=European+Union+OR+NATO+OR+%22Europe+politics%22+when:1d&hl=en-US&gl=US&ceid=US:en",
    # اخبار داخل ایران و دیپلماسی
    "https://news.google.com/rss/search?q=Iran+politics+OR+Tehran+when:1d&hl=en-US&gl=US&ceid=US:en"
]

DB_FILE = "sent_articles.json"

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not GEMINI_API_KEY:
    print("❌ خطا: متغیرهای Secret تنظیم نشده‌اند.")
    sys.exit(1)

client = genai.Client(api_key=GEMINI_API_KEY)
AVAILABLE_MODELS = ["gemini-3.8-flash"]

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
    # پرامپت حرفه‌ای و تخصصی برای اخبار جنگ، ژئوپلیتیک و سبک فوری شبکه X
    prompt = f"""
نقش تو: ادیتور ارشد و تحلیل‌گر ارشد میز خبر فوری (Breaking News) و مسائل استراتژیک بین‌الملل با تمرکز ویژه بر خاورمیانه، ایران و اروپا.

متن خام ورودی:
عنوان: {title}
خلاصه: {summary}

وظیفه:
خبر را با لحنی جدی، حرفه‌ای، بی‌طرف، جذاب و به سبک پست‌های تحلیلی/فوری شبکه X (توییتر) به زبان فارسی سلیس بنویس.

قالب خروجی (دقیقاً با همین ساختار باشد):
🚨 [یک تیتر کوتاه، تکان‌دهنده و جذاب به سبک Breaking News همراه ایموجی مرتبط]

🔹 جزئیات میدان و دیپلماسی:
- نکته اول: اصل رویداد (چه اتفاقی افتاده، بازیگران چه کسانی هستند؟)
- نکته دوم: واکنش طرف‌ها (موضع تهران، واشنگتن، تل‌آویو یا پایتخت‌های اروپایی)
- نکته سوم: شواهد میدانی، بیانیه‌ها یا پیامدهای نظامی/امنیتی مستقیم

⚠️ تحلیل و ارزیابی پیامد:
یک پاراگراف کوتاه (حداکثر دو جمله) که نشان دهد این تحول چه تأثیری بر موازنه قوا در منطقه، اقتصاد یا احتمال تشدید درگیری دارد.

نکات الزامی:
- ادعاها یا اخبار تأییدنشده را مشخص کن.
- از بزرگ‌نمایی بی‌مورد یا لحن احساسی پرهیز کن؛ فکت‌محور بنویس.
- هیچ پیش‌گفتار، سلام، توضیح یا متن اضافه‌ای خارج از قالب بالا ننویس.
"""
    for model_name in AVAILABLE_MODELS:
        for attempt in range(1, 4):
            try:
                response = client.models.generate_content(
                    model=model_name,
                    contents=prompt,
                )
                text = response.text.strip()
                return f"{text}\n\n🔗 [لینک منبع خبر]({link})"
            except Exception as e:
                err_str = str(e)
                if "503" in err_str:
                    time.sleep(6)
                else:
                    print(f"⚠️ خطای جمنای ({model_name}): {err_str}")
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
            # ارسال بدون مارک‌داون در صورت وجود کاراکترهای ناسازگار
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

def main():
    sent_articles = load_sent_articles()
    new_sent = False

    # جمع‌آوری خبرها از تمام فیدهای هدف
    all_entries = []
    for url in RSS_FEEDS:
        feed = feedparser.parse(url)
        all_entries.extend(feed.entries[:3])

    # حداکثر ۲ تا ۳ خبر در هر اجرا ارسال می‌شود تا هم سهمیه حفظ شود و هم کانال شلوغ نشود
    sent_count = 0
    max_news_per_run = 3

    for entry in all_entries:
        if sent_count >= max_news_per_run:
            break

        article_id = entry.get("id", entry.get("link"))
        if article_id not in sent_articles:
            print(f"در حال پردازش رویداد: {entry.get('title')}")
            summary = summarize_with_gemini(
                entry.get("title", ""),
                entry.get("summary", ""),
                entry.get("link", "")
            )
            if summary and send_to_telegram(summary):
                sent_articles.add(article_id)
                new_sent = True
                sent_count += 1
                save_sent_articles(sent_articles)

                # وقفه ۳ دقیقه‌ای (۱۸۰ ثانیه) بین ارسال هر خبر
                if sent_count < max_news_per_run:
                    print("⏳ انتظار به مدت ۳ دقیقه قبل از ارسال خبر بعدی...")
                    time.sleep(180)
        else:
            continue

    if new_sent:
        print("پایان چرخه؛ اخبار با وقفه ۳ دقیقه‌ای ارسال شدند.")
    else:
        print("تحول جدیدی خارج از اخبار قبلی پیدا نشد.")

if __name__ == "__main__":
    main()
