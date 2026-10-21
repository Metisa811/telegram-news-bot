import calendar
import html
import json
import os
import re
import subprocess
import sys
import time
from datetime import datetime, timezone
from urllib.parse import urlparse

import feedparser
import requests

TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
TELEGRAM_CHAT_ID = os.environ.get("TELEGRAM_CHAT_ID")
GROQ_API_KEY = os.environ.get("GROQ_API_KEY")

# بله (اختیاری): اگر این دو تنظیم شوند، خبرها در بله هم ارسال می‌شوند
BALE_BOT_TOKEN = os.environ.get("BALE_BOT_TOKEN")
BALE_CHAT_ID = os.environ.get("BALE_CHAT_ID")

if not TELEGRAM_BOT_TOKEN or not TELEGRAM_CHAT_ID or not GROQ_API_KEY:
    print("❌ خطا: متغیرهای Secret تنظیم نشده‌اند.")
    sys.exit(1)

# اول مدل قوی‌تر، اگر سهمیه‌اش تمام شد مدل بعدی
MODELS = ["openai/gpt-oss-120b", "openai/gpt-oss-20b", "qwen/qwen3.8-27b"]

CHANNEL_TAG = "@newsberoza"   # آیدی کانال؛ ته هر پیام با یک خط فاصله
DB_FILE = "sent_articles.json"       # شناسه‌ی خبرهای ارسال‌شده (هرگز پاک نمی‌شود)
ARCHIVE_FILE = "news_archive.jsonl"  # بایگانی کامل: منبع + متن اصلی + متن منتشرشده
GLOSSARY_FILE = "glossary.json"      # واژه‌نامه، قوانین نگارش و نمونه‌ها (RAG)
DB_MAX = 200000
INTERVAL_SECONDS = 120   # فاصله بین هر دور
MAX_SEND_PER_CYCLE = 1   # هر دور حداکثر چند خبر
MAX_TRIES_PER_CYCLE = 3  # هر دور حداکثر چند خبر را به هوش مصنوعی بده
GIT_SYNC_EVERY = 15      # هر چند دور، داده‌ها در گیت ذخیره شود (~۳۰ دقیقه)
RUN_MINUTES = float(os.environ.get("RUN_MINUTES", "330"))

TELEGRAM_CHANNELS = [
    "idfinfarsi",
    "withyashar",
    "IranintlTV",
    "BazChiShod",
]


def gnews(query, lang="en-US", country="US"):
    ceid = f"{country}:{lang.split('-')[0]}"
    return (f"https://news.google.com/rss/search?q={query}+when:1d"
            f"&hl={lang}&gl={country}&ceid={ceid}")


# منابع اولویت‌دار
PRIORITY_FEEDS = [
    gnews("Iran+OR+Israel+OR+Hezbollah+OR+%22Middle+East%22"),
    gnews("Iran+politics+OR+Tehran"),
    gnews("%22Middle+East%22+war+OR+strike+OR+missile"),
    gnews("site:now14.co.il", "he", "IL"),   # کانال ۱۴
    gnews("site:13tv.co.il", "he", "IL"),    # کانال ۱۳
    gnews("site:kan.org.il", "he", "IL"),    # کان ۱۱
    "https://moxie.foxnews.com/google-publisher/world.xml",
    gnews("site:foxnews.com+Iran+OR+Israel+OR+%22Middle+East%22"),
    gnews("site:centcom.mil"),
    gnews("CENTCOM+OR+%22Central+Command%22"),
    "https://www.timesofisrael.com/feed/",
    "https://www.jpost.com/rss/rssfeedsheadlines.aspx",
    "https://www.aljazeera.com/xml/rss/all.xml",
    "http://feeds.bbci.co.uk/news/world/middle_east/rss.xml",
]

MAX_AGE_HOURS = 12  # خبرهای RSS قدیمی‌تر از این نادیده گرفته می‌شوند

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


# ---------- واژه‌نامه و بازنویسی (RAG) ----------
def load_glossary():
    try:
        with open(GLOSSARY_FILE, "r", encoding="utf-8") as f:
            g = json.load(f)
            print(f"📚 واژه‌نامه بارگذاری شد: {len(g.get('entries', []))} مدخل")
            return g
    except Exception as e:
        print(f"⚠️ واژه‌نامه پیدا نشد ({e}). بدون واژه‌نامه ادامه می‌دهم.")
        return {"entries": [], "style_rules": [], "examples": []}


GLOSSARY = load_glossary()


def norm(s):
    """یکسان‌سازی حروف عربی/فارسی و حذف نیم‌فاصله و اعراب برای تطبیق."""
    s = (s or "").replace("ي", "ی").replace("ى", "ی").replace("ك", "ک")
    s = re.sub("[\u200c\u200e\u200f\u064b-\u065f]", "", s)
    return s.lower()


def term_in(term, ntext):
    t = norm(term).strip()
    if not t:
        return False
    if re.fullmatch(r"[a-z0-9 .\-']+", t):  # عبارت لاتین: با مرز کلمه
        return re.search(r"(?<![a-z0-9])" + re.escape(t) + r"(?![a-z0-9])", ntext) is not None
    return t in ntext


def retrieve(text, limit=12):
    """مدخل‌های واژه‌نامه که در متن خبر آمده‌اند."""
    nt = norm(text)
    found = []
    for e in GLOSSARY.get("entries", []):
        terms = [e["canonical"]] + e.get("aliases", []) + e.get("fix", [])
        if any(term_in(t, nt) for t in terms):
            found.append(e)
            if len(found) >= limit:
                break
    return found


def _fix_pairs():
    pairs = []
    for e in GLOSSARY.get("entries", []):
        for f in e.get("fix", []):
            pairs.append((f, e["canonical"]))
    pairs.sort(key=lambda p: len(p[0]), reverse=True)  # عبارت بلندتر اول
    return pairs


FIX_PAIRS = _fix_pairs()


def apply_fixes(text):
    """جایگزینی قطعی نام‌های غلط/عامیانه با نام رسمی."""
    for wrong, right in FIX_PAIRS:
        text = re.sub(re.escape(wrong), right, text, flags=re.I)
    return text


def glossary_block(text):
    lines = []
    for e in retrieve(text):
        line = f"- {e['canonical']}"
        if e.get("aliases"):
            line += f" (معادل‌ها/املاهای دیگر: {'، '.join(e['aliases'][:4])})"
        if e.get("note"):
            line += f" — {e['note']}"
        lines.append(line)
    return "\n".join(lines) if lines else "(مورد خاصی نیست)"


def examples_block():
    parts = []
    for ex in GLOSSARY.get("examples", [])[:2]:
        parts.append(f"ورودی:\n{ex['input']}\nخروجی:\n{ex['output']}")
    return "\n\n".join(parts) if parts else "(ندارد)"


def clean_output(text):
    text = apply_fixes(text or "")
    text = re.sub(r"https?://\S+", "", text)
    text = re.sub(r"@\w+", "", text)            # هیچ آیدی و کانالی در متن نباشد
    text = text.replace("*", "").replace("`", "")
    lines = []
    for ln in text.splitlines():
        s = ln.strip()
        if not s:
            lines.append("")
            continue
        if "تلگرام" in s or "t.me" in s.lower() or s.startswith("منبع"):
            continue
        s = re.sub(r"^[-–—]\s+", "• ", s)
        lines.append(s)
    text = "\n".join(lines)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    return text


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


def archive_news(item, final_text):
    rec = {
        "time": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "id": item["id"],
        "source": item.get("source", ""),
        "title": item["title"],
        "original": item["summary"][:1500],
        "text": final_text,
    }
    with open(ARCHIVE_FILE, "a", encoding="utf-8") as f:
        f.write(json.dumps(rec, ensure_ascii=False) + "\n")


def git_sync():
    """ذخیره‌ی داده‌ها در گیت هنگام اجرا روی GitHub Actions."""
    if os.environ.get("GITHUB_ACTIONS") != "true":
        return
    try:
        for path in (DB_FILE, ARCHIVE_FILE):
            if os.path.exists(path):
                subprocess.run(["git", "add", path], capture_output=True)
        r = subprocess.run(["git", "commit", "-m", "Update news data [skip ci]"],
                           capture_output=True, text=True)
        if r.returncode == 0:
            subprocess.run(["git", "pull", "--rebase", "--autostash"], capture_output=True)
            p = subprocess.run(["git", "push"], capture_output=True, text=True)
            print("💾 داده‌ها در گیت ذخیره شد." if p.returncode == 0
                  else f"⚠️ push ناموفق: {p.stderr[:150]}")
    except Exception as e:
        print(f"⚠️ خطای git: {e}")


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
                            "source": f"telegram:@{username}",
                        })
    except Exception as e:
        print(f"⚠️ خطا در کانال @{username}: {e}")
    return posts


def fetch_rss(url, limit, force_priority=False):
    items = []
    try:
        resp = requests.get(
            url, timeout=15,
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"})
        feed = feedparser.parse(resp.content)
        feed_title = (feed.feed.get("title") if feed.feed else "") or urlparse(url).netloc
        for entry in feed.entries[:limit]:
            pub = entry.get("published_parsed") or entry.get("updated_parsed")
            if pub and (time.time() - calendar.timegm(pub)) > MAX_AGE_HOURS * 3600:
                continue
            src = entry.get("source")
            src_title = src.get("title") if isinstance(src, dict) else None
            items.append({
                "id": entry.get("id", entry.get("link")),
                "title": clean_html(entry.get("title", "")),
                "summary": clean_html(entry.get("summary", "")),
                "priority": force_priority,
                "source": src_title or feed_title,
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
    title = apply_fixes(title)
    summary = apply_fixes(summary)[:1500]
    rules = "\n".join(f"- {r}" for r in GLOSSARY.get("style_rules", []))
    prompt = f"""
تو ویراستار یک کانال خبری فارسی‌زبان هستی. خبر زیر را (حتی اگر انگلیسی یا عبری است) به فارسی معیار، رسمی و امروزی بازنویسی کن؛ همان‌طور که خبرگزاری‌های حرفه‌ای فارسی می‌نویسند.

قالب خروجی (دقیقاً همین):
🚨 [تیتر کوتاه و رسمی]
• [فکت اول]
• [فکت دوم]
• [فکت سوم، فقط در صورت نیاز]

قوانین نگارش:
{rules}

قوانین ثابت:
- فقط خود خبر؛ هیچ تحلیل، نتیجه‌گیری، پیش‌بینی یا جمله درباره‌ی «اهمیت» یا «پیامد» خبر ننویس.
- نام افراد و نهادها را دقیقاً مطابق واژه‌نامه بنویس؛ لقب، کنیه و نام عامیانه ممنوع است.
- اگر خبر در متن به یک خبرگزاری یا رسانه‌ی معتبر نسبت داده شده (مثل رویترز، آسوشیتدپرس، ایرنا، تسنیم، کان، فاکس نیوز)، می‌توانی بنویسی «به گزارش …».
- هرگز نام کانال تلگرام، آیدی (@…)، لینک یا عبارتی مثل «پیام از کانال …» ننویس.
- فقط از اطلاعات خود متن استفاده کن؛ چیزی اضافه یا حدس نزن.
- حجم کل کمتر از ۶۰ کلمه.
- هیچ پیش‌گفتار یا توضیح خارج از قالب ننویس.

واژه‌نامه‌ی مرتبط با این خبر:
{glossary_block(title + " " + summary)}

نمونه‌های نگارش درست:
{examples_block()}

خبر:
تیتر: {title}
متن: {summary}
"""
    for model in MODELS:
        try:
            payload = {
                "model": model,
                "temperature": 0.2,
                "max_completion_tokens": 800,
                "messages": [{"role": "user", "content": prompt}],
            }
            if "gpt-oss" in model:
                payload["reasoning_effort"] = "low"  # مصرف توکن کمتر
            r = requests.post(
                "https://api.groq.com/openai/v1/chat/completions",
                headers={"Authorization": f"Bearer {GROQ_API_KEY}"},
                json=payload,
                timeout=45,
            )
            if r.status_code == 429:
                print(f"⏳ سهمیه {model} پر است، مدل بعدی...")
                continue
            if r.status_code != 200:
                print(f"⚠️ Groq {model}: {r.status_code} {r.text[:200]}")
                continue
            text = r.json()["choices"][0]["message"]["content"] or ""
            text = re.sub(r"<think>.*?</think>", "", text, flags=re.DOTALL)
            text = clean_output(text)
            if len(text) >= 20:
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
        "disable_web_page_preview": True,
    }
    try:
        res = requests.post(url, json=payload, timeout=15).json()
        if not res.get("ok"):
            print(f"⚠️ تلگرام: {res}")
            return False
        return True
    except Exception as e:
        print(f"⚠️ خطای تلگرام: {e}")
        return False


# ---------- بله ----------
def send_to_bale(message):
    if not BALE_BOT_TOKEN or not BALE_CHAT_ID:
        return False
    url = f"https://tapi.bale.ai/bot{BALE_BOT_TOKEN}/sendMessage"
    try:
        chat_id = int(BALE_CHAT_ID) if BALE_CHAT_ID.lstrip("-").isdigit() else BALE_CHAT_ID
        res = requests.post(
            url, json={"chat_id": chat_id, "text": message}, timeout=15)
        data = res.json()
        if not data.get("ok"):
            print(f"⚠️ بله: {res.status_code} {str(data)[:200]}")
            return False
        return True
    except Exception as e:
        print(f"⚠️ خطای بله: {e}")
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

    seen = set()
    unprocessed = []
    for c in candidates:
        if not c["id"] or c["id"] in sent_set or c["id"] in seen:
            continue
        seen.add(c["id"])
        unprocessed.append(c)

    unprocessed.sort(key=lambda c: not is_priority(c))
    print(f"خبر جدید: {len(unprocessed)}")

    sent_count = 0
    tries = 0
    for item in unprocessed:
        if sent_count >= MAX_SEND_PER_CYCLE or tries >= MAX_TRIES_PER_CYCLE:
            break
        tries += 1
        tag = "⭐" if is_priority(item) else "📰"
        print(f"{tag} پردازش: {item['title'][:50]}  [{item.get('source', '')}]")

        text = summarize(item["title"], item["summary"])
        if not text:
            continue
        final = f"{text}\n\n{CHANNEL_TAG}"   # یک خط فاصله و آیدی کانال
        tg_ok = send_to_telegram(final)
        bale_ok = send_to_bale(final)
        if tg_ok or bale_ok:
            print(f"✅ ارسال شد. (تلگرام: {tg_ok} | بله: {bale_ok})")
            sent_list.append(item["id"])
            save_sent(sent_list)
            archive_news(item, text)
            sent_count += 1


def main():
    deadline = time.time() + RUN_MINUTES * 60
    print(f"شروع؛ مدت اجرا: {RUN_MINUTES:.0f} دقیقه")
    cycle = 0
    while True:
        cycle += 1
        try:
            run_once()
        except Exception as e:
            print(f"⚠️ خطای غیرمنتظره: {e}")
        if cycle % GIT_SYNC_EVERY == 0:
            git_sync()
        remaining = deadline - time.time()
        if remaining <= 0:
            break
        time.sleep(min(INTERVAL_SECONDS, remaining))
    git_sync()
    print("پایان عملیات.")


if __name__ == "__main__":
    main()
